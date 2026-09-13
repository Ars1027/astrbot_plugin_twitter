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
            ids = [i["tweet_id"] for i in state["items"]]
            common = (
                state["version"] in (1, 2)
                and isinstance(state["generation"], str)
                and bool(state["generation"])
                and isinstance(state["source"], str)
                and str(state["anchor_since_id"]).isdigit()
                and state["phase"] in {"scanning", "ready", "blocked"}
                and isinstance(state["next_cursor"], str)
                and isinstance(state["seen_cursors"], list)
                and all(isinstance(c, str) for c in state["seen_cursors"])
                and isinstance(state["cursor_failures"], int)
                and state["cursor_failures"] >= 0
                and isinstance(state["blocked_reason"], str)
                and isinstance(state["items"], list)
                and len(ids) == len(set(ids))
                and all(
                    isinstance(i, dict)
                    and isinstance(i["tweet_id"], str)
                    and i["tweet_id"].isdigit()
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
                            and state["required"][k] >= 0
                            for k in ("items", "bytes", "cursors")
                        )
                    )
                )
            )
            if not common:
                return False
            if state["version"] == 1:
                return (
                    state["upper_id"] is None or str(state["upper_id"]).isdigit()
                ) and all(
                    int(state["anchor_since_id"]) < int(i) <= int(state["upper_id"])
                    for i in ids
                )
            return (
                isinstance(state["scan_order"], list)
                and len(state["scan_order"]) == len(set(state["scan_order"]))
                and set(state["scan_order"]).issubset(ids)
                and isinstance(state["acknowledged_ids"], list)
                and all(
                    isinstance(i, str) and i.isdigit()
                    for i in state["acknowledged_ids"]
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
                version=2,
                generation=uuid4().hex,
                source=self._source(),
                anchor_since_id=anchor,
                scan_order=[],
                acknowledged_ids=[],
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
        if state["version"] == 1:
            # v1 的上限可能已过滤掉合法转帖；保留条目并重新确认完整性。
            state.update(
                version=2,
                generation=uuid4().hex,
                scan_order=[],
                next_cursor="",
                seen_cursors=[],
                cursor_failures=0,
                acknowledged_ids=[
                    str(i)
                    for i in author.get("processed_tweet_ids", [])
                    if str(i).isdigit()
                ],
            )
            state.pop("upper_id")
            acknowledged = set(state["acknowledged_ids"])
            state["items"] = [
                i for i in state["items"] if i["tweet_id"] not in acknowledged
            ]
            if state["phase"] != "blocked":
                state["phase"] = "scanning"
            if not await self.subscriptions.save_timeline_backlog(
                username, saved, state
            ):
                return TimelineBatch([], True)
            saved = copy.deepcopy(state)
        # 已确认完整的队列跨来源也先交付，不按新来源重新扫描或重新排序。
        if state["phase"] == "ready":
            if not state["items"]:
                await self.subscriptions.save_timeline_backlog(username, saved, None)
            return TimelineBatch(list(reversed(state["items"])))
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
                scan_order=[],
                cursor_failures=0,
                phase="scanning",
            )
        for _ in range(self.MAX_PAGES):
            if state["phase"] == "ready":
                break
            try:
                page = await self.api.get_user_timeline_page(
                    username,
                    cursor=state["next_cursor"],
                    since_id=state["anchor_since_id"],
                )
            except TwitterTimelineError:
                if state["next_cursor"]:
                    state["cursor_failures"] += 1
                await self.subscriptions.save_timeline_backlog(username, saved, state)
                raise
            candidate = copy.deepcopy(state)
            lower = int(state["anchor_since_id"])
            items = {i["tweet_id"]: i for i in candidate["items"]}
            ordered = set(candidate["scan_order"])
            acknowledged = set(candidate["acknowledged_ids"])
            for item in page.items:
                tweet_id = item["tweet_id"]
                if int(tweet_id) > lower and tweet_id not in acknowledged:
                    items.setdefault(tweet_id, item)
                    if tweet_id not in ordered:
                        candidate["scan_order"].append(tweet_id)
                        ordered.add(tweet_id)
            candidate["items"] = list(items.values())
            complete = page.exhausted or any(
                (int(i["tweet_id"]) <= lower and not i["is_retweet"])
                or (self.api.provider == "fxtwitter" and int(i["tweet_id"]) == lower)
                for i in page.items
            )
            if complete:
                # 未重新定位的旧条目放在新到旧列表尾部，交付时优先且保留相对顺序。
                order = candidate["scan_order"] + [i for i in items if i not in ordered]
                candidate["items"] = [items[i] for i in order]
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
            self.api.cache_timeline_items(
                page, set(items) - {i["tweet_id"] for i in saved["items"]}
            )
            state = candidate
            saved = copy.deepcopy(state)
            if looping:
                return TimelineBatch([], True)
        if state["phase"] != "ready":
            return TimelineBatch([], True)
        if not state["items"]:
            await self.subscriptions.save_timeline_backlog(username, saved, None)
        return TimelineBatch(list(reversed(state["items"])))
