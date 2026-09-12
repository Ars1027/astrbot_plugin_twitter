"""完整扫描后再交给发送层，分页进度与发送游标分别持久化。"""

import asyncio
import copy
import json
from dataclasses import dataclass
from uuid import uuid4

from astrbot.api import logger

from ..twitter_api import TwitterTimelineError


@dataclass(frozen=True)
class TimelineBatch:
    items: list[dict]
    pending: bool = False


class TimelineBacklogService:
    MAX_PAGES = 4
    MAX_ITEMS = 5000
    MAX_BYTES = 8 * 1024 * 1024
    MAX_CURSORS = 1000

    def __init__(self, twitter_api, subscriptions):
        self.api = twitter_api
        self.subscriptions = subscriptions
        self._lock = asyncio.Lock()

    def _source(self) -> str:
        provider = self.api.provider
        base = (
            self.api.fxtwitter_api_base
            if provider == "fxtwitter"
            else self.api.nitter_url
        )
        return f"{provider}:{base.rstrip('/')}"

    @staticmethod
    def _valid(state) -> bool:
        if not isinstance(state, dict):
            return False
        try:
            return (
                state["version"] == 1
                and isinstance(state["generation"], str)
                and bool(state["generation"])
                and isinstance(state["source"], str)
                and str(state["anchor_since_id"]).isdigit()
                and (state["upper_id"] is None or str(state["upper_id"]).isdigit())
                and state["phase"] in {"scanning", "ready", "blocked"}
                and isinstance(state["next_cursor"], str)
                and isinstance(state["seen_cursors"], list)
                and all(isinstance(c, str) for c in state["seen_cursors"])
                and isinstance(state["cursor_failures"], int)
                and state["cursor_failures"] >= 0
                and isinstance(state["items"], list)
                and all(
                    isinstance(i, dict)
                    and str(i["tweet_id"]).isdigit()
                    and state["upper_id"] is not None
                    and int(state["anchor_since_id"])
                    < int(i["tweet_id"])
                    <= int(state["upper_id"])
                    and isinstance(i["is_retweet"], bool)
                    and all(
                        isinstance(i[k], str)
                        for k in (
                            "username",
                            "retweeter_username",
                            "retweeter_screen_name",
                        )
                    )
                    for i in state["items"]
                )
                and (
                    state["phase"] != "blocked"
                    or (
                        isinstance(state["required"], dict)
                        and all(
                            isinstance(state["required"][k], int)
                            for k in ("items", "bytes", "cursors")
                        )
                    )
                )
            )
        except (KeyError, TypeError, ValueError):
            return False

    async def get_batch(self, username: str) -> TimelineBatch:
        # 同一服务的并发检查不重复扫描；KV 锁仅在快照读写期间持有。
        async with self._lock:
            return await self._get_batch(username)

    async def _get_batch(self, username: str) -> TimelineBatch:
        snapshot = await self.subscriptions.get_snapshot()
        key = self.subscriptions.find_key(snapshot, username)
        if key is None:
            return TimelineBatch([])
        author = snapshot[key]
        state = author.get("timeline_backlog")
        if state is None:
            anchor = str(author.get("since_id") or "0")
            if not anchor.isdigit():
                logger.error(f"@{username} 游标无效，暂停分页")
                return TimelineBatch([], True)
            state = dict(
                version=1,
                generation=uuid4().hex,
                source=self._source(),
                anchor_since_id=anchor,
                upper_id=None,
                phase="scanning",
                next_cursor="",
                seen_cursors=[],
                items=[],
                cursor_failures=0,
                blocked_reason="",
            )
            if not await self.subscriptions.save_timeline_backlog(
                username, None, state, expected_since_id=anchor
            ):
                return TimelineBatch([], True)
        if not self._valid(state):
            logger.error(f"@{username} 积压格式损坏或版本不支持，保留数据并暂停")
            return TimelineBatch([], True)

        saved = copy.deepcopy(state)
        if state["phase"] == "blocked":
            required = state["required"]
            if (
                required["items"] > self.MAX_ITEMS
                or required["bytes"] > self.MAX_BYTES
                or required["cursors"] > self.MAX_CURSORS
            ):
                logger.error(
                    f"@{username} 积压暂停: {state['blocked_reason']}，已保存 {len(state['items'])} 条"
                )
                return TimelineBatch([], True)
            state["phase"] = "scanning"
            state["blocked_reason"] = ""
            state.pop("required")
        if state["source"] != self._source() or state["cursor_failures"] >= 3:
            state.update(
                generation=uuid4().hex,
                source=self._source(),
                next_cursor="",
                seen_cursors=[],
                cursor_failures=0,
                phase="scanning",
            )
        for _ in range(self.MAX_PAGES):
            if state["phase"] == "ready":
                break
            try:
                page = await self.api.get_user_timeline_page(
                    username, cursor=state["next_cursor"]
                )
            except TwitterTimelineError:
                if state["next_cursor"]:
                    state["cursor_failures"] += 1
                await self.subscriptions.save_timeline_backlog(username, saved, state)
                raise
            candidate = copy.deepcopy(state)
            if candidate["upper_id"] is None and page.items:
                newest = max(int(i["tweet_id"]) for i in page.items)
                if newest > int(state["anchor_since_id"]):
                    candidate["upper_id"] = newest
            lower, upper = int(state["anchor_since_id"]), candidate["upper_id"]
            items = {i["tweet_id"]: i for i in candidate["items"]}
            for item in page.items:
                if lower < int(item["tweet_id"]) <= int(upper or 0):
                    items.setdefault(item["tweet_id"], item)
            candidate["items"] = list(items.values())
            complete = page.exhausted or any(
                int(i["tweet_id"]) <= lower and not i["is_retweet"] for i in page.items
            )
            candidate["cursor_failures"] = 0
            candidate["seen_cursors"].append(state["next_cursor"])
            candidate["next_cursor"] = page.next_cursor or ""
            candidate["phase"] = "ready" if complete else "scanning"
            looping = (
                not complete and candidate["next_cursor"] in candidate["seen_cursors"]
            )
            if looping:
                candidate["cursor_failures"] = 3
            required = dict(
                items=len(items),
                bytes=len(json.dumps(candidate).encode()),
                cursors=len(candidate["seen_cursors"]),
            )
            if (
                required["items"] > self.MAX_ITEMS
                or required["bytes"] > self.MAX_BYTES
                or required["cursors"] > self.MAX_CURSORS
            ):
                # 不保存半页，也不确认这一页的 next_cursor。
                state.update(
                    phase="blocked",
                    required=required,
                    blocked_reason="达到积压容量或扫描上限",
                )
                await self.subscriptions.save_timeline_backlog(username, saved, state)
                logger.error(
                    f"@{username} 积压达到上限，保留 {len(state['items'])} 条并暂停"
                )
                return TimelineBatch([], True)
            if not await self.subscriptions.save_timeline_backlog(
                username, saved, candidate
            ):
                return TimelineBatch([], True)
            state = candidate
            saved = copy.deepcopy(state)
            if looping:
                return TimelineBatch([], True)
        if state["phase"] != "ready":
            return TimelineBatch([], True)
        if not state["items"]:
            await self.subscriptions.save_timeline_backlog(username, saved, None)
        return TimelineBatch(sorted(state["items"], key=lambda i: int(i["tweet_id"])))
