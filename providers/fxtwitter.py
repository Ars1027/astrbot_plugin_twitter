"""FxTwitter 请求、分页与适配；方法使用 TwitterAPI 实例的共享状态。"""

import asyncio
import re
from datetime import datetime
from time import perf_counter
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from astrbot.api import logger

from ..twitter_article import attach_article, parse_fxtwitter_article
from ..twitter_diagnostics import anchor, exception_type_chain, reference
from . import DATA_PROVIDER_FXTWITTER, FxTwitterTimelineError

DEFAULT_FXTWITTER_API_BASE = "https://api.fxtwitter.com"

FXTWITTER_MAX_TIMELINE_PAGES = 4
FXTWITTER_MAX_TIMELINE_ITEMS = 100
FXTWITTER_STATUS_CACHE_SIZE = 200

FXTWITTER_REQUEST_HEADERS = {
    "User-Agent": (
        "AstrBot-Twitter-Plugin/1.8 "
        "(+https://github.com/Ars1027/astrbot_plugin_twitter)"
    ),
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class FxTwitterProviderMixin:
    """内部方法分组；配置、HTTP 客户端和缓存由 TwitterAPI 管理。"""

    async def _request_fxtwitter_json(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        retries: int = 2,
    ) -> Optional[dict]:
        """请求 FxTwitter JSON，统一处理超时、限流、5xx 与解码错误。"""
        url = f"{self.fxtwitter_api_base}/{str(path or '').lstrip('/')}"
        attempts = max(1, int(retries))

        for attempt in range(attempts):
            client = await self._get_client()
            started = perf_counter()
            try:
                resp = await client.get(url, params=params)
            except httpx.TimeoutException as e:
                logger.debug(anchor(
                    "fx.request.failed", request=reference(path), attempt=attempt + 1,
                    elapsed=round(perf_counter() - started, 2), causes=exception_type_chain(e),
                ))
                logger.warning(f"FxTwitter API 连接或读取超时: {url}, {e}")
                client_invalidated = await self._invalidate_client(client)
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                if client_invalidated:
                    self.provider_ready = False
                return None
            except httpx.RequestError as e:
                logger.debug(anchor(
                    "fx.request.failed", request=reference(path), attempt=attempt + 1,
                    elapsed=round(perf_counter() - started, 2), causes=exception_type_chain(e),
                ))
                logger.warning(f"FxTwitter API 请求失败: {url}, {e}")
                client_invalidated = await self._invalidate_client(client)
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                if client_invalidated:
                    self.provider_ready = False
                return None

            if resp.status_code == 429:
                retry_after = str(resp.headers.get("Retry-After") or "").strip()
                suffix = f"，Retry-After={retry_after}" if retry_after else ""
                logger.warning(f"FxTwitter API 触发限流: {url}{suffix}")
                self.provider_ready = False
                return None

            if 500 <= resp.status_code < 600:
                logger.warning(
                    f"FxTwitter API 服务端错误: {url}, 状态码: {resp.status_code}, "
                    f"尝试 {attempt + 1}/{attempts}"
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                # 5xx 也可能只针对某个账号或推文（如私密账号时间线），
                # 不能据此把整个数据源标为不可用并阻断其他订阅。
                return None

            if resp.status_code < 200 or resp.status_code >= 300:
                logger.warning(
                    f"FxTwitter API 请求失败: {url}, 状态码: {resp.status_code}"
                )
                return None

            try:
                payload = resp.json()
            except ValueError:
                summary = re.sub(r"\s+", " ", resp.text[:240]).strip()
                logger.warning(
                    f"FxTwitter API JSON 解码失败: {url}, 状态码: "
                    f"{resp.status_code}, 响应摘要: {summary!r}"
                )
                self.provider_ready = False
                return None

            if not isinstance(payload, dict):
                logger.warning(f"FxTwitter API 返回非对象 JSON: {url}")
                self.provider_ready = False
                return None

            api_code = payload.get("code")
            try:
                api_ok = int(api_code) == 200
            except (TypeError, ValueError):
                api_ok = False
            if not api_ok:
                message = str(payload.get("message") or "")[:160]
                logger.warning(
                    f"FxTwitter API 内部错误: {url}, code={api_code}, "
                    f"message={message!r}"
                )
                return None
            self.provider_ready = True
            return payload

        return None

    async def check_fxtwitter_available(self) -> bool:
        """轻量检查 FxTwitter 时间线接口及响应契约。"""
        if self.provider != DATA_PROVIDER_FXTWITTER:
            return False

        payload = await self._request_fxtwitter_json(
            "2/profile/elonmusk/statuses",
            params={"count": 1},
        )
        available = bool(payload and isinstance(payload.get("results"), list))
        self.provider_ready = available
        if available:
            logger.info(f"FxTwitter API 可用: {self.fxtwitter_api_base}")
        else:
            logger.warning(f"FxTwitter API 不可用: {self.fxtwitter_api_base}")
        return available

    async def _get_fxtwitter_user_info(self, username: str) -> dict:
        """将 FxTwitter 用户资料转换成插件既有结构。"""
        username = str(username or "").strip().lstrip("@")
        failure = {
            "status": False,
            "screen_name": "",
            "bio": "",
            "user_name": username,
        }
        if not username:
            return failure

        payload = await self._request_fxtwitter_json(
            f"2/profile/{quote(username, safe='')}"
        )
        user = payload.get("user") if payload else None
        if not isinstance(user, dict):
            return failure

        user_name = str(user.get("screen_name") or username).strip().lstrip("@")
        screen_name = str(user.get("name") or user_name).strip()
        return {
            "status": bool(user_name),
            "screen_name": screen_name,
            "bio": str(user.get("description") or ""),
            "user_name": user_name or username,
        }

    @staticmethod
    def _flatten_fxtwitter_results(results: list) -> list[dict]:
        """兼容普通状态列表和可选的 thread 分组响应。"""
        flattened: list[dict] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "thread":
                statuses = item.get("statuses") or []
                if isinstance(statuses, list):
                    flattened.extend(
                        status
                        for status in statuses
                        if isinstance(status, dict) and status.get("type") == "status"
                    )
            elif item.get("type") == "status":
                flattened.append(item)
        return flattened

    def _cache_fxtwitter_status(self, status: dict) -> None:
        tweet_id = str(status.get("id") or "")
        if not tweet_id:
            return
        cached_status = dict(status)
        cached_status.pop("reposted_by", None)
        self._status_cache[tweet_id] = cached_status
        self._status_cache.move_to_end(tweet_id)
        while len(self._status_cache) > FXTWITTER_STATUS_CACHE_SIZE:
            self._status_cache.popitem(last=False)

    @staticmethod
    def _fxtwitter_timeline_item(status: dict, requested_username: str) -> dict:
        author = status.get("author") or {}
        reposted_by = status.get("reposted_by") or {}
        is_retweet = isinstance(reposted_by, dict) and bool(
            reposted_by.get("screen_name") or reposted_by.get("name")
        )
        return {
            "tweet_id": str(status.get("id") or ""),
            "username": str(author.get("screen_name") or requested_username)
            .strip()
            .lstrip("@"),
            "is_retweet": is_retweet,
            "retweeter_username": str(
                reposted_by.get("screen_name") or requested_username
            )
            .strip()
            .lstrip("@"),
            "retweeter_screen_name": str(
                reposted_by.get("name")
                or reposted_by.get("screen_name")
                or requested_username
            ).strip(),
        }

    async def _get_fxtwitter_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0,
        *, first_page_only: bool = False,
    ) -> list[dict]:
        """获取完整的 FxTwitter 时间线增量，并保持既有排序语义。"""
        username = str(username or "").strip().lstrip("@")
        since_id = str(since_id or "").strip()
        if not username:
            return []

        try:
            since_int = int(since_id) if since_id else None
        except ValueError:
            logger.warning(
                f"检测到无效 since_id @{username}: {since_id!r}，"
                "为避免回放历史，本轮不获取时间线"
            )
            return []

        items: list[dict] = []
        statuses_to_cache: list[dict] = []
        seen_ids: set[str] = set()
        cursor = ""
        previous_cursor = ""
        boundary_found = False
        timeline_exhausted = False
        requested_limit_reached = False
        local_item_limit_reached = False

        for page_index in range(self.fxtwitter_max_pages):
            params: dict[str, Any] = {"count": 20}
            if cursor:
                params["cursor"] = cursor

            payload = await self._request_fxtwitter_json(
                f"2/profile/{quote(username, safe='')}/statuses",
                params=params,
            )
            if payload is None:
                page_label = "首页" if page_index == 0 else f"第 {page_index + 1} 页"
                raise FxTwitterTimelineError(
                    f"获取 @{username} 的 FxTwitter 时间线失败：{page_label}请求失败"
                )

            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise FxTwitterTimelineError(
                    f"获取 @{username} 的 FxTwitter 时间线失败："
                    f"第 {page_index + 1} 页响应结构异常"
                )

            statuses = self._flatten_fxtwitter_results(raw_results)
            for status in statuses:
                tweet_id = str(status.get("id") or "")
                if not tweet_id or tweet_id in seen_ids or status.get("is_pinned") is True:
                    continue
                seen_ids.add(tweet_id)

                item = self._fxtwitter_timeline_item(status, username)
                try:
                    tweet_int = int(tweet_id)
                except ValueError:
                    continue

                if since_int is not None and tweet_int <= since_int:
                    if tweet_int == since_int or not item.get("is_retweet"):
                        boundary_found = True
                    continue

                statuses_to_cache.append(status)
                items.append(item)

                if since_int is None and limit > 0 and len(items) >= limit:
                    requested_limit_reached = True
                    break
                if len(seen_ids) >= self.fxtwitter_max_items:
                    local_item_limit_reached = True
                    break

            if boundary_found or requested_limit_reached or (first_page_only and since_int is None):
                break
            if local_item_limit_reached:
                if since_int is not None:
                    raise FxTwitterTimelineError(
                        f"获取 @{username} 的 FxTwitter 时间线失败：达到本地抓取上限，"
                        "尚未找到上次游标"
                    )
                break

            cursor_info = payload.get("cursor")
            if cursor_info is not None and not isinstance(cursor_info, dict):
                raise FxTwitterTimelineError(
                    f"获取 @{username} 的 FxTwitter 时间线失败："
                    f"第 {page_index + 1} 页分页游标结构异常"
                )
            next_cursor = (
                str(cursor_info.get("bottom") or "")
                if isinstance(cursor_info, dict)
                else ""
            )
            if not next_cursor:
                timeline_exhausted = True
                break
            if next_cursor == cursor or next_cursor == previous_cursor:
                logger.warning(anchor(
                    "timeline.cursor.repeated", author=username, page=page_index + 1,
                    cursor=reference(cursor), next_cursor=reference(next_cursor),
                    previous_cursor=reference(previous_cursor), accepted=len(items),
                ))
                raise FxTwitterTimelineError(
                    f"获取 @{username} 的 FxTwitter 时间线失败：分页游标重复"
                )
            previous_cursor, cursor = cursor, next_cursor

        if since_int is not None and not boundary_found and not timeline_exhausted:
            raise FxTwitterTimelineError(
                f"获取 @{username} 的 FxTwitter 时间线失败："
                f"在 {self.fxtwitter_max_pages} 页内尚未找到上次游标"
            )

        for status in statuses_to_cache:
            self._cache_fxtwitter_status(status)
        return self._finalize_fxtwitter_items(items, since_int, limit)

    @staticmethod
    def _finalize_fxtwitter_items(
        items: list[dict], since_int: Optional[int], limit: int
    ) -> list[dict]:
        if since_int is not None:
            items.sort(key=lambda item: int(str(item.get("tweet_id") or "0")))
        if limit > 0:
            return items[:limit]
        return items

    @staticmethod
    def _format_fxtwitter_date(value: Any) -> str:
        """解析 FxTwitter 的 Twitter 时间并转为运行主机本地时区。"""
        value = str(value or "").strip()
        if not value:
            return ""
        try:
            parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
            return parsed.astimezone().isoformat(sep=" ", timespec="seconds")
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.astimezone().isoformat(sep=" ", timespec="seconds")
            except ValueError:
                return value

    @staticmethod
    def _fxtwitter_verified(author: dict) -> bool:
        verification = author.get("verification") or {}
        if isinstance(verification, dict):
            return bool(verification.get("verified"))
        return bool(author.get("verified"))

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            seconds = max(0, int(round(float(value))))
        except (TypeError, ValueError):
            return ""
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _apply_fxtwitter_image_quality(self, url: str) -> str:
        """将 FxTwitter 图片 URL 调整为配置的 orig/large 质量。"""
        url = str(url or "").strip()
        if not url or self.image_quality == "orig":
            return url
        try:
            parts = urlsplit(url)
            if not parts.netloc.lower().endswith("twimg.com"):
                return url
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            updated = False
            new_query: list[tuple[str, str]] = []
            for key, value in query_items:
                if key == "name":
                    value = "large"
                    updated = True
                new_query.append((key, value))
            if not updated:
                new_query.append(("name", "large"))
            return urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(new_query), parts.fragment)
            )
        except Exception:
            return url

    @staticmethod
    def _deduplicate_media_entries(entries: list) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("id") or entry.get("url") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(entry)
        return result

    @staticmethod
    def _select_fxtwitter_video_url(video: dict) -> str:
        """优先选择最高码率 MP4，再回退到媒体 URL、转码或流媒体。"""
        formats = video.get("formats") or []
        mp4_formats = [
            item
            for item in formats
            if isinstance(item, dict)
            and str(item.get("url") or "").startswith(("http://", "https://"))
            and (
                str(item.get("container") or "").lower() == "mp4"
                or ".mp4" in str(item.get("url") or "").lower()
            )
        ]
        if mp4_formats:
            best = max(
                mp4_formats,
                key=lambda item: (
                    int(item.get("bitrate") or 0),
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                ),
            )
            return str(best.get("url") or "")

        for key in ("url", "transcode_url"):
            candidate = str(video.get(key) or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate

        for item in formats:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("url") or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
        return ""

    def _extract_fxtwitter_media(
        self, status: dict
    ) -> tuple[list[str], list[str], list[dict]]:
        media = status.get("media") or {}
        if not isinstance(media, dict):
            return [], [], []

        all_entries = media.get("all") or []
        photos = media.get("photos") or []
        videos = media.get("videos") or []
        if not isinstance(photos, list) or not photos:
            photos = [
                item
                for item in all_entries
                if isinstance(item, dict) and item.get("type") == "photo"
            ]
        if not isinstance(videos, list) or not videos:
            videos = [
                item
                for item in all_entries
                if isinstance(item, dict) and item.get("type") in ("video", "gif")
            ]

        image_urls: list[str] = []
        for photo in self._deduplicate_media_entries(photos):
            image_url = self._apply_fxtwitter_image_quality(
                str(photo.get("url") or "")
            )
            if image_url and image_url not in image_urls:
                image_urls.append(image_url)

        video_urls: list[str] = []
        video_previews: list[dict] = []
        for video in self._deduplicate_media_entries(videos):
            video_url = self._select_fxtwitter_video_url(video)
            if video_url and video_url not in video_urls:
                video_urls.append(video_url)
            poster = str(video.get("thumbnail_url") or "").strip()
            if poster and not any(item.get("poster") == poster for item in video_previews):
                video_previews.append(
                    {
                        "poster": poster,
                        "duration": self._format_duration(video.get("duration")),
                    }
                )

        external = media.get("external") or {}
        if isinstance(external, dict):
            poster = str(external.get("thumbnail_url") or "").strip()
            if poster and not any(item.get("poster") == poster for item in video_previews):
                video_previews.append({"poster": poster, "duration": ""})

        if media.get("broadcast"):
            logger.info(
                f"检测到 FxTwitter 直播/广播 @{(status.get('author') or {}).get('screen_name', '')}/"
                f"{status.get('id', '')}，过滤媒体内容"
            )
            return [], [], []

        return image_urls, video_urls, video_previews

    def _adapt_fxtwitter_content(
        self, status: dict, fallback_username: str = "", fallback_id: str = ""
    ) -> dict:
        """适配主贴和引用帖共有的内容，不处理统计或转帖上下文。"""
        author = status.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        images, videos, previews = self._extract_fxtwitter_media(status)
        username = str(author.get("screen_name") or fallback_username).lstrip("@")
        result = {
            "author": str(author.get("name") or author.get("screen_name") or ""),
            "screen_name": str(author.get("name") or username),
            "username": username,
            "avatar": str(author.get("avatar_url") or ""),
            "verified": self._fxtwitter_verified(author),
            "date": self._format_fxtwitter_date(status.get("created_at")),
            "tweet_id": str(status.get("id") or fallback_id),
            "text": str(status.get("text") or ""),
            "images": images,
            "videos": videos,
            "video_previews": previews,
        }
        attach_article(result, parse_fxtwitter_article(
            status.get("article"), self._apply_fxtwitter_image_quality,
            self._select_fxtwitter_video_url,
        ))
        return result

    def _adapt_fxtwitter_quote(self, status: Any) -> Optional[dict]:
        if not isinstance(status, dict) or status.get("type") != "status":
            return None
        result = self._adapt_fxtwitter_content(status)
        result.pop("screen_name")
        return result

    def _adapt_fxtwitter_status(
        self, status: dict, fallback_username: str = "", fallback_id: str = ""
    ) -> dict:
        content = self._adapt_fxtwitter_content(status, fallback_username, fallback_id)
        username = content["username"]
        tweet_id = content["tweet_id"]
        result = self._empty_tweet_result(username, tweet_id)
        result.update(content)
        result.pop("author")
        reposted_by = status.get("reposted_by") or {}
        retweet = None
        if isinstance(reposted_by, dict) and (
            reposted_by.get("screen_name") or reposted_by.get("name")
        ):
            retweet = {
                "retweeter_username": str(
                    reposted_by.get("screen_name") or ""
                ).lstrip("@"),
                "retweeter_screen_name": str(
                    reposted_by.get("name")
                    or reposted_by.get("screen_name")
                    or ""
                ),
            }

        def stat_value(key: str) -> str:
            value = status.get(key)
            return "" if value is None else str(value)

        result.update(
            {
                "status": status.get("type") == "status" and bool(tweet_id),
                "username": username,
                "stats": {
                    "comments": stat_value("replies"),
                    "retweets": stat_value("reposts"),
                    "likes": stat_value("likes"),
                    "views": stat_value("views"),
                },
                "quote": self._adapt_fxtwitter_quote(status.get("quote")),
                "retweet": retweet,
                "is_r18": bool(status.get("possibly_sensitive")),
                "url": str(status.get("url") or result["url"]),
                "replying_to": (
                    status.get("replying_to")
                    if isinstance(status.get("replying_to"), dict)
                    else None
                ),
            }
        )
        return result

    async def _get_fxtwitter_tweet(self, username: str, tweet_id: str) -> dict:
        tweet_id = str(tweet_id or "").strip()
        username = str(username or "").strip().lstrip("@")
        result = self._empty_tweet_result(username, tweet_id)
        if not tweet_id.isdigit():
            return result

        status = self._status_cache.get(tweet_id)
        article = status.get("article") if isinstance(status, dict) else None
        quote_article = (status.get("quote") or {}).get("article") if isinstance(status, dict) else None
        needs_article_detail = any(
            isinstance(a, dict) and not (a.get("content") or {}).get("blocks")
            for a in (article, quote_article)
        )
        if status is None or needs_article_detail:
            payload = await self._request_fxtwitter_json(f"2/status/{tweet_id}")
            detail = payload.get("status") if payload else None
            if isinstance(detail, dict) and detail.get("type") == "status":
                status = detail
        if not isinstance(status, dict) or status.get("type") != "status":
            return result

        self._cache_fxtwitter_status(status)
        return self._adapt_fxtwitter_status(status, username, tweet_id)

    async def get_tweet_translation(
        self, username: str, tweet_id: str, lang: str
    ) -> Optional[dict]:
        """获取 FxTwitter 原生译文，不修改原始详情缓存。"""
        tweet_id = str(tweet_id or "").strip()
        lang = str(lang or "").strip()
        if self.provider != DATA_PROVIDER_FXTWITTER or not tweet_id.isdigit() or not lang:
            return None
        payload = await self._request_fxtwitter_json(
            f"2/status/{tweet_id}", params={"lang": lang}
        )
        status = payload.get("status") if isinstance(payload, dict) else None
        if not isinstance(status, dict) or status.get("type") != "status":
            return None

        def translated_text(item):
            translation = item.get("translation") if isinstance(item, dict) else None
            text = translation.get("text") if isinstance(translation, dict) else None
            return text if isinstance(text, str) and text.strip() else None

        result = {}
        text = translated_text(status)
        if text:
            result["text"] = text
        quote_text = translated_text(status.get("quote"))
        if quote_text:
            result["quote"] = {"text": quote_text}
        return result or None
