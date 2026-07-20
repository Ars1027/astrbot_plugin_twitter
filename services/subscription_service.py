"""订阅关系、轮询游标和转帖去重记录的持久化服务。"""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from typing import Any


KV_SUBS_KEY = "twitter_subs"
KV_RETWEET_DEDUP_KEY = "twitter_retweet_dedup_seen"
RETWEET_DEDUP_MAX_ITEMS = 500
PROCESSED_TWEET_MAX_ITEMS = 500

KVGetter = Callable[[str, Any], Awaitable[Any]]
KVSetter = Callable[[str, Any], Awaitable[None]]
ProviderReady = Callable[[], bool]


class SubscriptionService:
    """集中管理插件的订阅持久化和并发写入。"""

    def __init__(
        self,
        get_kv_data: KVGetter,
        put_kv_data: KVSetter,
        twitter_api: Any,
        provider_ready: ProviderReady,
    ) -> None:
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self.twitter_api = twitter_api
        self._provider_ready = provider_ready
        self._lock = asyncio.Lock()

    async def get_all(self) -> dict:
        """获取全部订阅数据。"""
        data = await self._get_kv_data(KV_SUBS_KEY, {})
        return data if isinstance(data, dict) else {}

    async def save_all(self, data: dict) -> None:
        """保存全部订阅数据。"""
        await self._put_kv_data(KV_SUBS_KEY, data)

    async def get_snapshot(self) -> dict:
        """获取不会被并发写入影响的订阅快照。"""
        async with self._lock:
            return copy.deepcopy(await self.get_all())

    @staticmethod
    def find_key(subs: dict, username: str) -> str | None:
        """按不区分大小写的用户名查找现有推主键。"""
        expected = username.casefold()
        return next(
            (key for key in subs if str(key).casefold() == expected),
            None,
        )

    async def add(
        self,
        umo: str,
        username: str,
        *,
        r18: bool = False,
        media_only: bool = False,
        reject_duplicate: bool = False,
    ) -> dict:
        """新增订阅关系，并在可能时复用已有推主数据。"""
        username = str(username or "").strip().lstrip("@")
        session_config = {
            "status": True,
            "r18": bool(r18),
            "media": bool(media_only),
        }

        async with self._lock:
            subs = await self.get_all()
            existing_key = self.find_key(subs, username)
            if existing_key is not None:
                author = subs[existing_key]
                subscribers = author.setdefault("subscribers", {})
                if reject_duplicate and umo in subscribers:
                    return {
                        "ok": False,
                        "reason": "duplicate",
                        "username": existing_key,
                    }
                replaced = umo in subscribers
                subscribers[umo] = session_config
                await self.save_all(subs)
                return {
                    "ok": True,
                    "username": existing_key,
                    "screen_name": str(
                        author.get("screen_name") or existing_key
                    ),
                    "bio": "",
                    "created_author": False,
                    "replaced": replaced,
                }

        if not self._provider_ready():
            return {"ok": False, "reason": "provider_unavailable"}

        user_info = await self.twitter_api.get_user_info(username)
        if not isinstance(user_info, dict) or not user_info.get("status"):
            return {"ok": False, "reason": "not_found"}

        latest_ids = await self.twitter_api.get_user_newtimeline(username)
        since_id = str(latest_ids[-1]) if latest_ids else ""
        screen_name = str(user_info.get("screen_name") or username)

        # 网络请求期间可能已有另一个入口添加了同一推主，保存前再次检查。
        async with self._lock:
            subs = await self.get_all()
            existing_key = self.find_key(subs, username)
            if existing_key is not None:
                author = subs[existing_key]
                subscribers = author.setdefault("subscribers", {})
                if reject_duplicate and umo in subscribers:
                    return {
                        "ok": False,
                        "reason": "duplicate",
                        "username": existing_key,
                    }
                replaced = umo in subscribers
                subscribers[umo] = session_config
                await self.save_all(subs)
                return {
                    "ok": True,
                    "username": existing_key,
                    "screen_name": str(
                        author.get("screen_name") or screen_name
                    ),
                    "bio": str(user_info.get("bio") or ""),
                    "created_author": False,
                    "replaced": replaced,
                }

            subs[username] = {
                "screen_name": screen_name,
                "since_id": since_id,
                "processed_tweet_ids": [since_id] if since_id else [],
                "subscribers": {umo: session_config},
            }
            await self.save_all(subs)

        return {
            "ok": True,
            "username": username,
            "screen_name": screen_name,
            "bio": str(user_info.get("bio") or ""),
            "created_author": True,
            "replaced": False,
        }

    async def update(self, umo: str, username: str, changes: dict) -> dict:
        """修改单个会话中的订阅选项。"""
        async with self._lock:
            subs = await self.get_all()
            key = self.find_key(subs, username)
            if key is None:
                return {"ok": False, "reason": "not_found"}
            subscriber = subs[key].get("subscribers", {}).get(umo)
            if not isinstance(subscriber, dict):
                return {"ok": False, "reason": "not_found"}

            field_map = {
                "enabled": "status",
                "r18": "r18",
                "media_only": "media",
            }
            for field, value in changes.items():
                target = field_map.get(field)
                if target is not None:
                    subscriber[target] = value
            await self.save_all(subs)
            return {"ok": True, "username": key}

    async def remove(self, umo: str, username: str) -> dict:
        """移除订阅关系，并清理没有订阅者的推主。"""
        async with self._lock:
            subs = await self.get_all()
            key = self.find_key(subs, username)
            if key is None:
                return {"ok": False, "reason": "not_found"}
            subscribers = subs[key].get("subscribers", {})
            if umo not in subscribers:
                return {"ok": False, "reason": "not_found"}

            subscribers.pop(umo)
            author_removed = not subscribers
            if author_removed:
                subs.pop(key)
            await self.save_all(subs)
            return {
                "ok": True,
                "username": key,
                "author_removed": author_removed,
            }

    async def set_session_status(self, umo: str, enabled: bool) -> int:
        """统一修改一个会话中的全部推送状态。"""
        async with self._lock:
            subs = await self.get_all()
            count = 0
            for author in subs.values():
                subscriber = author.get("subscribers", {}).get(umo)
                if isinstance(subscriber, dict):
                    subscriber["status"] = enabled
                    count += 1
            if count:
                await self.save_all(subs)
            return count

    async def clear(self) -> dict:
        """清空全部订阅并返回清理前统计。"""
        async with self._lock:
            subs = await self.get_all()
            result = {
                "authors": len(subs),
                "relations": sum(
                    len(info.get("subscribers", {}))
                    for info in subs.values()
                ),
            }
            if subs:
                await self.save_all({})
            return result

    async def update_cursor(self, username: str, since_id: str) -> bool:
        """在订阅锁内单调推进游标，避免并发写入造成回退。"""
        next_id = str(since_id or "").strip()
        if not next_id.isdigit():
            return False

        async with self._lock:
            subs = await self.get_all()
            key = self.find_key(subs, username)
            if key is None:
                return False
            current_id = str(subs[key].get("since_id") or "").strip()
            if current_id.isdigit() and int(next_id) <= int(current_id):
                return True
            subs[key]["since_id"] = next_id
            await self.save_all(subs)
            return True

    @staticmethod
    def processed_tweet_ids(author_info: dict) -> set[str]:
        """读取某个推主最近已处理的时间线条目 ID。"""
        raw_ids = author_info.get("processed_tweet_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []

        processed_ids = {str(item) for item in raw_ids if str(item)}
        since_id = str(author_info.get("since_id") or "").strip()
        if since_id:
            processed_ids.add(since_id)
        return processed_ids

    @staticmethod
    def _append_processed_tweet_ids(
        author_info: dict,
        tweet_ids: list[str],
    ) -> bool:
        """追加已处理 ID 并限制长度，返回数据是否发生变化。"""
        raw_ids = author_info.get("processed_tweet_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []

        processed_ids = [str(item) for item in raw_ids if str(item)]
        original_ids = list(processed_ids)
        for tweet_id in tweet_ids:
            normalized_id = str(tweet_id or "").strip()
            if not normalized_id:
                continue
            if normalized_id in processed_ids:
                processed_ids.remove(normalized_id)
            processed_ids.append(normalized_id)

        processed_ids = processed_ids[-PROCESSED_TWEET_MAX_ITEMS:]
        if processed_ids == original_ids and isinstance(
            author_info.get("processed_tweet_ids"),
            list,
        ):
            return False
        author_info["processed_tweet_ids"] = processed_ids
        return True

    async def commit_processed_tweets(
        self,
        username: str,
        tweet_ids: list[str],
        since_id: str,
    ) -> bool:
        """原子记录已处理条目并单调推进轮询游标。"""
        normalized_ids = [
            str(tweet_id).strip()
            for tweet_id in tweet_ids
            if str(tweet_id).strip().isdigit()
        ]
        next_id = str(since_id or "").strip()
        if not normalized_ids and not next_id.isdigit():
            return False

        async with self._lock:
            subs = await self.get_all()
            key = self.find_key(subs, username)
            if key is None:
                return False

            author_info = subs[key]
            changed = self._append_processed_tweet_ids(
                author_info,
                normalized_ids,
            )
            current_id = str(author_info.get("since_id") or "").strip()
            if next_id.isdigit() and (
                not current_id.isdigit() or int(next_id) > int(current_id)
            ):
                author_info["since_id"] = next_id
                changed = True

            if changed:
                await self.save_all(subs)
            return True

    async def get_retweet_seen(self) -> dict:
        """获取转帖去重记录。"""
        data = await self._get_kv_data(KV_RETWEET_DEDUP_KEY, {})
        return data if isinstance(data, dict) else {}

    async def save_retweet_seen(self, data: dict) -> None:
        """保存转帖去重记录。"""
        await self._put_kv_data(KV_RETWEET_DEDUP_KEY, data)

    @staticmethod
    def retweet_seen_by_umo(seen_data: dict, umo: str, tweet_id: str) -> bool:
        """判断某会话是否已经接收过指定原帖的转帖。"""
        seen_ids = seen_data.get(umo) or []
        if not isinstance(seen_ids, list):
            return False
        return str(tweet_id) in {str(item) for item in seen_ids}

    @staticmethod
    def mark_retweet_seen(seen_data: dict, umo: str, tweet_id: str) -> None:
        """记录某会话已接收过指定原帖的转帖，并限制缓存长度。"""
        tweet_id = str(tweet_id or "")
        if not tweet_id:
            return

        raw_seen_ids = seen_data.get(umo) or []
        if not isinstance(raw_seen_ids, list):
            raw_seen_ids = []

        seen_ids = [str(item) for item in raw_seen_ids if str(item)]
        if tweet_id in seen_ids:
            seen_ids.remove(tweet_id)
        seen_ids.append(tweet_id)
        seen_data[umo] = seen_ids[-RETWEET_DEDUP_MAX_ITEMS:]
