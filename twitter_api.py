"""Twitter 数据访问入口，统一管理配置、HTTP 客户端和缓存。"""

import asyncio
import base64
import re
from collections import OrderedDict
from typing import Optional

import httpx
from astrbot.api import logger

from .providers import (
    DATA_PROVIDER_FXTWITTER as DATA_PROVIDER_FXTWITTER,
    DATA_PROVIDER_NITTER as DATA_PROVIDER_NITTER,
    DATA_PROVIDER_OPTIONS as DATA_PROVIDER_OPTIONS,
    FxTwitterTimelineError as FxTwitterTimelineError,
    TwitterTimelineError as TwitterTimelineError,
)
from .providers.fxtwitter import (
    DEFAULT_FXTWITTER_API_BASE as DEFAULT_FXTWITTER_API_BASE,
    FXTWITTER_MAX_TIMELINE_ITEMS as FXTWITTER_MAX_TIMELINE_ITEMS,
    FXTWITTER_MAX_TIMELINE_PAGES as FXTWITTER_MAX_TIMELINE_PAGES,
    FXTWITTER_REQUEST_HEADERS as FXTWITTER_REQUEST_HEADERS,
    FXTWITTER_STATUS_CACHE_SIZE as FXTWITTER_STATUS_CACHE_SIZE,
    FxTwitterProviderMixin,
)
from .providers.nitter import (
    BROADCAST_LINK_PATTERN as BROADCAST_LINK_PATTERN,
    NITTER_REQUEST_HEADERS as NITTER_REQUEST_HEADERS,
    WEBSITE_LIST as WEBSITE_LIST,
    NitterProviderMixin,
)

IMAGE_QUALITY_OPTIONS = ("large", "orig")


class TwitterAPI(NitterProviderMixin, FxTwitterProviderMixin):
    """对外保持 Nitter/FxTwitter 共用接口，内部按数据源分组实现。"""

    def __init__(
        self,
        proxy: Optional[str] = None,
        nitter_url: str = "",
        image_quality: str = "orig",
        provider: str = DATA_PROVIDER_NITTER,
        fxtwitter_api_base: str = DEFAULT_FXTWITTER_API_BASE,
        fxtwitter_max_pages: int = FXTWITTER_MAX_TIMELINE_PAGES,
        fxtwitter_max_items: int = FXTWITTER_MAX_TIMELINE_ITEMS,
    ):
        self.proxy = proxy
        self.nitter_url = nitter_url
        self.image_quality = image_quality if image_quality in IMAGE_QUALITY_OPTIONS else "orig"
        provider = str(provider or DATA_PROVIDER_NITTER).strip().lower()
        self.provider = (
            provider if provider in DATA_PROVIDER_OPTIONS else DATA_PROVIDER_NITTER
        )
        self.fxtwitter_api_base = (
            str(fxtwitter_api_base or DEFAULT_FXTWITTER_API_BASE).strip().rstrip("/")
            or DEFAULT_FXTWITTER_API_BASE
        )
        self.fxtwitter_max_pages = max(1, min(int(fxtwitter_max_pages), 10))
        self.fxtwitter_max_items = max(1, min(int(fxtwitter_max_items), 500))
        self.provider_ready = False
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        self._status_cache: OrderedDict[str, dict] = OrderedDict()

    @property
    def is_ready(self) -> bool:
        """当前数据源是否已通过初始化检查。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return self.provider_ready
        return bool(self.nitter_url)

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                proxy = self.proxy if self.proxy else None
                headers = (
                    FXTWITTER_REQUEST_HEADERS
                    if self.provider == DATA_PROVIDER_FXTWITTER
                    else NITTER_REQUEST_HEADERS
                )
                self._client = httpx.AsyncClient(
                    proxy=proxy,
                    http2=True,
                    timeout=httpx.Timeout(
                        connect=10.0,
                        read=30.0,
                        write=30.0,
                        pool=10.0,
                    ),
                    follow_redirects=True,
                    headers=dict(headers),
                )
            return self._client

    async def _invalidate_client(
        self,
        failed_client: httpx.AsyncClient,
    ) -> bool:
        """仅淘汰发生传输错误的当前客户端。"""
        async with self._client_lock:
            if self._client is not failed_client:
                return False
            self._client = None

        if not failed_client.is_closed:
            try:
                await failed_client.aclose()
            except Exception as exc:
                logger.debug(f"关闭异常 HTTP 客户端失败: {exc}")
        return True

    async def download_media(
        self, url: str, *, timeout: float = 30.0, max_bytes: int | None = None
    ) -> bytes:
        """通过已配置代理的 HTTP 客户端下载媒体文件。

        将远程 URL 转换为本地字节数据，避免下游消费者（消息适配器、
        HTML 渲染器）直连可能受网络限制的外部服务器。

        参数:
            url: 远程媒体文件 URL

        返回:
            媒体文件的原始字节数据

        异常:
            ValueError: URL 为空
            httpx.HTTPStatusError: 响应状态码非 2xx
        """
        url = str(url or "").strip()
        if not url:
            raise ValueError("empty URL")

        client = await self._get_client()
        if max_bytes is not None:
            async with client.stream(
                "GET", url, timeout=timeout, headers={"Accept-Encoding": "identity"}
            ) as resp:
                resp.raise_for_status()
                declared_size = resp.headers.get("Content-Length", "")
                if declared_size.isdigit() and int(declared_size) > max_bytes:
                    raise ValueError("媒体文件超过下载大小限制")
                data = bytearray()
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > max_bytes:
                        raise ValueError("媒体文件超过下载大小限制")
                    data.extend(chunk)
                return bytes(data)
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    async def download_media_to_data_uri(self, url: str) -> str:
        """下载媒体文件并转换为 data URI（base64 内嵌）。

        用于截图模式的 HTML 渲染——将图片转为 data: URI 后内嵌到 HTML，
        Playwright/Chromium 渲染时无需发起外部网络请求。

        参数:
            url: 远程媒体文件 URL

        返回:
            data: URI 字符串，如 data:image/png;base64,...

        异常:
            ValueError: URL 为空
            httpx.HTTPStatusError: 响应状态码非 2xx
        """
        data = await self.download_media(url)
        mime = self._guess_mime_from_url(url)
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _guess_mime_from_url(url: str) -> str:
        """根据 URL 后缀推测 MIME 类型，默认 image/jpeg。"""
        url_lower = str(url or "").lower()
        for ext, mime in [
            (".png", "image/png"),
            (".gif", "image/gif"),
            (".webp", "image/webp"),
            (".svg", "image/svg+xml"),
            (".bmp", "image/bmp"),
            (".mp4", "video/mp4"),
        ]:
            if ext in url_lower:
                return mime
        return "image/jpeg"

    async def close(self):
        """关闭 HTTP 客户端"""
        async with self._client_lock:
            client = self._client
            self._client = None
        if client and not client.is_closed:
            await client.aclose()

    @staticmethod
    def _parse_content_length(value: str) -> Optional[int]:
        """解析正数形式的 Content-Length 响应头。"""
        try:
            length = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return length if length > 0 else None

    @staticmethod
    def _parse_content_range_total(value: str) -> Optional[int]:
        """从 Content-Range 响应头解析文件总大小。"""
        match = re.search(r"/(\d+)\s*$", str(value or ""))
        if not match:
            return None
        try:
            total = int(match.group(1))
        except ValueError:
            return None
        return total if total > 0 else None

    async def get_remote_file_size(self, url: str) -> Optional[int]:
        """尽量在不下载正文的情况下探测远程文件大小。"""
        url = str(url or "").strip()
        if not url:
            return None

        client = await self._get_client()
        try:
            resp = await client.head(url, timeout=15.0)
            if resp.status_code < 400:
                size = self._parse_content_length(resp.headers.get("content-length", ""))
                if size is not None:
                    return size
        except Exception as e:
            logger.debug(f"HEAD 探测远程文件大小失败: {url}, {e}")

        try:
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-0"},
                timeout=15.0,
            ) as resp:
                if resp.status_code >= 400:
                    return None
                size = self._parse_content_range_total(
                    resp.headers.get("content-range", "")
                )
                if size is not None:
                    return size
                if resp.status_code == 206:
                    return None
                return self._parse_content_length(resp.headers.get("content-length", ""))
        except Exception as e:
            logger.debug(f"Range 探测远程文件大小失败: {url}, {e}")
            return None

    async def get_user_info(self, username: str) -> dict:
        """获取用户资料，保持两个数据源共用的返回结构。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_user_info(username)
        return await self._get_nitter_user_info(username)

    async def get_user_newtimeline(self, username: str, since_id: str = "") -> list[str]:
        """获取用户比 since_id 更新的推文 ID 列表

        Nitter 时间线按最新优先排列，返回结果按时间正序（最旧在前）。

        参数:
            username: 推主用户名
            since_id: 已知最新推文 ID，仅返回比此 ID 更新的推文；
                      为空时返回首批有效条目中的最大原帖 ID（用于首次订阅定位）

        返回:
            新推文 ID 列表（时间正序），无新推文时返回空列表
        """
        items = await self.get_user_timeline_items(
            username,
            since_id=since_id,
            first_page_only=not since_id,
        )
        ids = [str(item.get("tweet_id") or "") for item in items if str(item.get("tweet_id") or "").isdigit()]
        # 新转发可以位于顶部，但其原帖 ID 可能远小于同页已有的原创帖。
        return [max(ids, key=int)] if not since_id and ids else ids

    async def get_user_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0,
        *, first_page_only: bool = False,
    ) -> list[dict]:
        """获取时间线条目，包含转帖元数据。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_timeline_items(
                username, since_id=since_id, limit=limit, first_page_only=first_page_only,
            )
        return await self._get_nitter_timeline_items(username, since_id=since_id, limit=limit)

    @staticmethod
    def _empty_tweet_result(username: str, tweet_id: str) -> dict:
        username = str(username or "").strip().lstrip("@")
        tweet_id = str(tweet_id or "").strip()
        return {
            "status": False,
            "tweet_id": tweet_id,
            "username": username,
            "screen_name": username,
            "avatar": "",
            "verified": False,
            "date": "",
            "stats": {},
            "text": "",
            "images": [],
            "videos": [],
            "video_previews": [],
            "quote": None,
            "retweet": None,
            "is_r18": False,
            "url": (
                f"https://x.com/{username}/status/{tweet_id}"
                if username and tweet_id
                else ""
            ),
            "replying_to": None,
        }

    async def get_tweet(self, username: str, tweet_id: str) -> dict:
        """获取推文详情，保持两个数据源共用的返回结构。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_tweet(username, tweet_id)
        return await self._get_nitter_tweet(username, tweet_id)


def get_next_website(website_list: list[str], current: str) -> Optional[str]:
    """获取列表中当前镜像站的下一个（循环）"""
    if not website_list:
        return None
    try:
        idx = website_list.index(current)
        return website_list[(idx + 1) % len(website_list)]
    except ValueError:
        return website_list[0]
