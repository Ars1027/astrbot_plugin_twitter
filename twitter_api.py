"""
Twitter API 交互模块
通过 Nitter HTML 或 FxTwitter JSON API 获取 Twitter/X 推文数据
"""

import asyncio
import base64
import re
from collections import OrderedDict
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag
from astrbot.api import logger

# 内置 Nitter 镜像站列表
WEBSITE_LIST = [
    "https://nitter.net",
]

# 有效的图片质量选项
IMAGE_QUALITY_OPTIONS = ("large", "orig")

DATA_PROVIDER_NITTER = "nitter"
DATA_PROVIDER_FXTWITTER = "fxtwitter"
DATA_PROVIDER_OPTIONS = (DATA_PROVIDER_NITTER, DATA_PROVIDER_FXTWITTER)
DEFAULT_FXTWITTER_API_BASE = "https://api.fxtwitter.com"
FXTWITTER_MAX_TIMELINE_PAGES = 4
FXTWITTER_MAX_TIMELINE_ITEMS = 100
FXTWITTER_STATUS_CACHE_SIZE = 200

# 直播推文链接特征（推文链接中包含此路径即为直播）
BROADCAST_LINK_PATTERN = re.compile(r'/i/broadcasts/', re.IGNORECASE)


class TwitterAPI:
    """Twitter 数据访问层，向上提供兼容的 Nitter/FxTwitter 接口。"""

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
        self._status_cache: OrderedDict[str, dict] = OrderedDict()

    @property
    def is_ready(self) -> bool:
        """当前数据源是否已通过初始化检查。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return self.provider_ready
        return bool(self.nitter_url)

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            proxy = self.proxy if self.proxy else None
            self._client = httpx.AsyncClient(
                proxy=proxy,
                http2=True,
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "AstrBot-Twitter-Plugin/1.8 "
                        "(+https://github.com/Ars1027/astrbot_plugin_twitter)"
                    ),
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
        return self._client

    async def _request_fxtwitter_json(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        retries: int = 2,
    ) -> Optional[dict]:
        """请求 FxTwitter JSON，统一处理超时、限流、5xx 与解码错误。"""
        client = await self._get_client()
        url = f"{self.fxtwitter_api_base}/{str(path or '').lstrip('/')}"
        attempts = max(1, int(retries))

        for attempt in range(attempts):
            try:
                resp = await client.get(url, params=params)
            except httpx.TimeoutException as e:
                logger.warning(f"FxTwitter API 连接或读取超时: {url}, {e}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return None
            except httpx.RequestError as e:
                logger.warning(f"FxTwitter API 请求失败: {url}, {e}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return None

            if resp.status_code == 429:
                retry_after = str(resp.headers.get("Retry-After") or "").strip()
                suffix = f"，Retry-After={retry_after}" if retry_after else ""
                logger.warning(f"FxTwitter API 触发限流: {url}{suffix}")
                return None

            if 500 <= resp.status_code < 600:
                logger.warning(
                    f"FxTwitter API 服务端错误: {url}, 状态码: {resp.status_code}, "
                    f"尝试 {attempt + 1}/{attempts}"
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
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
                return None

            if not isinstance(payload, dict):
                logger.warning(f"FxTwitter API 返回非对象 JSON: {url}")
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
            return payload

        return None

    async def download_media(self, url: str) -> bytes:
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
        resp = await client.get(url, timeout=30.0)
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
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

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

    async def check_website_available(self, website_list: list[str]) -> Optional[str]:
        """检测可用的镜像站，返回第一个可用的 URL"""
        if self.provider != DATA_PROVIDER_NITTER:
            return None

        client = await self._get_client()
        for url in website_list:
            try:
                test_url = f"{url}/elonmusk"
                resp = await client.get(test_url, timeout=15.0)
                if resp.status_code == 200:
                    logger.info(f"Nitter 镜像站可用: {url}")
                    self.nitter_url = url
                    self.provider_ready = True
                    return url
                logger.debug(f"Nitter 镜像站不可用: {url}, 状态码: {resp.status_code}")
            except Exception as e:
                logger.debug(f"Nitter 镜像站检测异常: {url}, 错误: {e}")
                continue
        logger.warning("所有 Nitter 镜像站均不可用")
        self.provider_ready = False
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

    async def get_user_info(self, username: str) -> dict:
        """获取 Twitter 用户信息

        返回:
            {"status": bool, "screen_name": str, "bio": str, "user_name": str}
        """
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_user_info(username)

        if not self.nitter_url:
            return {"status": False, "screen_name": "", "bio": "", "user_name": username}

        client = await self._get_client()
        url = f"{self.nitter_url}/{username}"
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                return {"status": False, "screen_name": "", "bio": "", "user_name": username}

            soup = BeautifulSoup(resp.text, "html.parser")

            name_elem = soup.select_one("a.profile-card-fullname")
            screen_name = name_elem.get_text(strip=True) if name_elem else username

            bio_elem = soup.select_one("div.profile-bio")
            bio = bio_elem.get_text(strip=True) if bio_elem else ""

            return {
                "status": True,
                "screen_name": screen_name,
                "bio": bio,
                "user_name": username,
            }
        except Exception as e:
            logger.error(f"获取用户信息失败 {username}: {e}")
            return {"status": False, "screen_name": "", "bio": "", "user_name": username}

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

    async def get_user_newtimeline(self, username: str, since_id: str = "") -> list[str]:
        """获取用户比 since_id 更新的推文 ID 列表

        Nitter 时间线按最新优先排列，返回结果按时间正序（最旧在前）。

        参数:
            username: 推主用户名
            since_id: 已知最新推文 ID，仅返回比此 ID 更新的推文；
                      为空时仅返回最新一条推文 ID（用于首次订阅定位）

        返回:
            新推文 ID 列表（时间正序），无新推文时返回空列表
        """
        items = await self.get_user_timeline_items(
            username,
            since_id=since_id,
            limit=1 if not since_id else 0,
        )
        return [str(item.get("tweet_id") or "") for item in items if item.get("tweet_id")]

    def _parse_timeline_items(
        self,
        soup: BeautifulSoup,
        username: str,
        since_id: str = "",
        limit: int = 0,
    ) -> list[dict]:
        """解析用户时间线条目。

        有 since_id 时返回时间正序（最旧在前）；无 since_id 时保持 Nitter 页面顺序
        （最新在前），便于测试指令向后寻找下一条非转帖。
        """
        timeline_items = soup.select("div.timeline-item")
        parsed_items: list[dict] = []

        for item in timeline_items:
            # 检测置顶推文标记并跳过
            if item.select_one(".pinned, .icon-pin"):
                continue

            link = item.select_one("a.tweet-link")
            if not link:
                continue

            href = link.get("href", "")
            match = re.search(r"/([^/]+)/status/(\d+)", href)
            if not match:
                continue

            tweet_username = match.group(1)
            tweet_id = match.group(2)
            retweet_header = item.select_one(".retweet-header")

            if since_id:
                try:
                    if int(tweet_id) <= int(since_id):
                        if retweet_header:
                            continue
                        # 时间线按最新优先，遇到 <= since_id 的即可停止
                        break
                except ValueError:
                    continue

            retweeter_screen_name = ""
            if retweet_header:
                retweeter_screen_name = (
                    retweet_header.get_text(" ", strip=True)
                    .replace("retweeted", "")
                    .strip()
                )

            parsed_items.append(
                {
                    "tweet_id": tweet_id,
                    "username": tweet_username or item.get("data-username") or username,
                    "is_retweet": retweet_header is not None,
                    "retweeter_username": username,
                    "retweeter_screen_name": retweeter_screen_name,
                }
            )

            if limit > 0 and len(parsed_items) >= limit:
                break

        if since_id:
            parsed_items.reverse()
        return parsed_items

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
        self._status_cache[tweet_id] = status
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
        self, username: str, since_id: str = "", limit: int = 0
    ) -> list[dict]:
        """获取 FxTwitter 时间线，有限分页、去重并保持既有排序语义。"""
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
        seen_ids: set[str] = set()
        cursor = ""
        previous_cursor = ""
        boundary_found = False

        for page_index in range(self.fxtwitter_max_pages):
            params: dict[str, Any] = {"count": 20}
            if cursor:
                params["cursor"] = cursor

            payload = await self._request_fxtwitter_json(
                f"2/profile/{quote(username, safe='')}/statuses",
                params=params,
            )
            if payload is None:
                if page_index > 0:
                    logger.warning(
                        f"FxTwitter 时间线分页中断 @{username}: 已完成 {page_index} 页"
                    )
                return [] if page_index == 0 else self._finalize_fxtwitter_items(
                    items, since_int, limit
                )

            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                logger.warning(f"FxTwitter 时间线 results 不是列表 @{username}")
                return [] if page_index == 0 else self._finalize_fxtwitter_items(
                    items, since_int, limit
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

                self._cache_fxtwitter_status(status)
                items.append(item)

                if since_int is None and limit > 0 and len(items) >= limit:
                    return items[:limit]
                if len(seen_ids) >= self.fxtwitter_max_items:
                    logger.debug(
                        f"FxTwitter 时间线达到本地抓取上限 @{username}: "
                        f"{self.fxtwitter_max_items}"
                    )
                    boundary_found = True
                    break

            if boundary_found:
                break

            cursor_info = payload.get("cursor") or {}
            next_cursor = (
                str(cursor_info.get("bottom") or "")
                if isinstance(cursor_info, dict)
                else ""
            )
            if not next_cursor or next_cursor == cursor or next_cursor == previous_cursor:
                break
            previous_cursor, cursor = cursor, next_cursor

        if since_int is not None and not boundary_found and cursor:
            logger.debug(
                f"FxTwitter 时间线在 {self.fxtwitter_max_pages} 页内未找到 "
                f"since_id @{username}: {since_id}"
            )
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

    async def get_user_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0
    ) -> list[dict]:
        """获取用户时间线条目，包含转帖元数据。"""
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_timeline_items(
                username, since_id=since_id, limit=limit
            )

        if not self.nitter_url:
            return []

        client = await self._get_client()
        url = f"{self.nitter_url}/{username}"
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            return self._parse_timeline_items(
                soup,
                username=username,
                since_id=since_id,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"获取用户时间线失败 {username}: {e}")
            return []

    def _build_image_url(self, a_href: str, img_src: str) -> str:
        """根据图片质量配置构建图片 URL
        orig 原图：使用 <a> href（Nitter /pic/orig/ 路由，追加 name=orig&format=jpg）
        large 缩略图：直接使用 <img> src 原样返回（Nitter 默认缩略图，webp 格式）
        """
        if self.image_quality == "orig":
            return a_href
        return img_src

    def _absolute_url(self, url: str) -> str:
        """将 Nitter 相对路径转换为绝对 URL。"""
        if not url or url.startswith("http"):
            return url
        return f"{self.nitter_url}{url}"

    @staticmethod
    def _is_nested_quote_element(tag: Tag, root: Tag) -> bool:
        """判断元素是否位于 root 内部的引用帖容器中。"""
        for parent in tag.parents:
            if parent is root:
                return False
            classes = parent.get("class") or []
            if "quote" in classes:
                return True
        return False

    def _extract_images(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> list[str]:
        """从指定容器提取图片 URL。"""
        images: list[str] = []
        attachments = container.select("a.still-image")
        for a_tag in attachments:
            if not include_nested_quotes and self._is_nested_quote_element(
                a_tag, container
            ):
                continue
            a_href = a_tag.get("href", "")
            img = a_tag.select_one("img")
            img_src = img.get("src", "") if img else ""
            src = self._build_image_url(a_href, img_src)
            if src:
                images.append(self._absolute_url(src))
        return images

    def _extract_videos(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> list[str]:
        """从指定容器提取视频/GIF URL。"""
        videos: list[str] = []
        video_elems = container.select("div.attachment video")
        seen_urls: set[str] = set()
        for video in video_elems:
            if not include_nested_quotes and self._is_nested_quote_element(
                video, container
            ):
                continue
            for source in video.find_all("source"):
                src = source.get("src", "")
                if src:
                    src = self._absolute_url(src)
                    if src not in seen_urls:
                        seen_urls.add(src)
                        videos.append(src)

            src = video.get("src", "")
            if src:
                src = self._absolute_url(src)
                if src not in seen_urls:
                    seen_urls.add(src)
                    videos.append(src)

            data_url = video.get("data-url", "")
            if data_url:
                data_url = self._absolute_url(data_url)
                if data_url not in seen_urls:
                    seen_urls.add(data_url)
                    videos.append(data_url)
        return videos

    def _extract_video_previews(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> list[dict]:
        """提取截图渲染用的视频封面图。"""
        previews: list[dict] = []
        seen_posters: set[str] = set()
        video_elems = container.select("div.attachment video")
        for video in video_elems:
            if not include_nested_quotes and self._is_nested_quote_element(
                video, container
            ):
                continue
            poster = self._absolute_url(video.get("poster", ""))
            if poster and poster not in seen_posters:
                seen_posters.add(poster)
                previews.append({"poster": poster, "duration": ""})

        overlay_elems = container.select("div.video-overlay")
        for overlay in overlay_elems:
            if not include_nested_quotes and self._is_nested_quote_element(
                overlay, container
            ):
                continue
            attachment = overlay.find_parent("div", class_="attachment")
            img = attachment.select_one("img") if attachment else None
            poster = self._absolute_url(img.get("src", "")) if img else ""
            duration_elem = overlay.select_one(".overlay-duration")
            duration = duration_elem.get_text(strip=True) if duration_elem else ""
            if poster and poster not in seen_posters:
                seen_posters.add(poster)
                previews.append({"poster": poster, "duration": duration})
        return previews

    def _extract_avatar(self, container: Tag) -> str:
        """从 Nitter 推文容器提取头像 URL。"""
        avatar_img = container.select_one("a.tweet-avatar img, img.avatar")
        if not avatar_img:
            return ""
        return self._absolute_url(avatar_img.get("src", ""))

    def _has_verified_badge(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> bool:
        """判断推文容器中是否存在可见的认证标记。"""
        for badge in container.select(".verified-icon"):
            if not include_nested_quotes and self._is_nested_quote_element(
                badge, container
            ):
                continue
            return True
        return False

    @staticmethod
    def _extract_date(container: Tag) -> str:
        """Extract the visible tweet date from a Nitter tweet container."""
        date_link = container.select_one("span.tweet-date a")
        if not date_link:
            return ""
        return date_link.get_text(strip=True) or date_link.get("title", "")

    @staticmethod
    def _extract_stats(container: Tag) -> dict:
        """Extract visible tweet stats from Nitter's action row."""
        stats = {"comments": "", "retweets": "", "likes": "", "views": ""}
        stat_elems = container.select("div.tweet-stats span.tweet-stat")
        stat_keys = ("comments", "retweets", "likes", "views")
        for key, stat_elem in zip(stat_keys, stat_elems):
            text = stat_elem.get_text(" ", strip=True)
            stats[key] = text
        return stats

    def _contains_live_stream(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> bool:
        """检测容器内是否包含直播链接。"""
        for link in container.select("a"):
            if not include_nested_quotes and self._is_nested_quote_element(
                link, container
            ):
                continue
            href = link.get("href", "")
            if href and BROADCAST_LINK_PATTERN.search(href):
                return True
        return False

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

    def _adapt_fxtwitter_quote(self, status: Any) -> Optional[dict]:
        if not isinstance(status, dict) or status.get("type") != "status":
            return None
        author = status.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        images, videos, previews = self._extract_fxtwitter_media(status)
        return {
            "author": str(author.get("name") or author.get("screen_name") or ""),
            "username": str(author.get("screen_name") or "").lstrip("@"),
            "avatar": str(author.get("avatar_url") or ""),
            "verified": self._fxtwitter_verified(author),
            "date": self._format_fxtwitter_date(status.get("created_at")),
            "tweet_id": str(status.get("id") or ""),
            "text": str(status.get("text") or ""),
            "images": images,
            "videos": videos,
            "video_previews": previews,
        }

    def _adapt_fxtwitter_status(
        self, status: dict, fallback_username: str = "", fallback_id: str = ""
    ) -> dict:
        author = status.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        username = str(author.get("screen_name") or fallback_username).lstrip("@")
        tweet_id = str(status.get("id") or fallback_id)
        result = self._empty_tweet_result(username, tweet_id)
        images, videos, previews = self._extract_fxtwitter_media(status)
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
                "screen_name": str(author.get("name") or username),
                "avatar": str(author.get("avatar_url") or ""),
                "verified": self._fxtwitter_verified(author),
                "date": self._format_fxtwitter_date(status.get("created_at")),
                "stats": {
                    "comments": stat_value("replies"),
                    "retweets": stat_value("reposts"),
                    "likes": stat_value("likes"),
                    "views": stat_value("views"),
                },
                "text": str(status.get("text") or ""),
                "images": images,
                "videos": videos,
                "video_previews": previews,
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
        if status is None:
            payload = await self._request_fxtwitter_json(f"2/status/{tweet_id}")
            status = payload.get("status") if payload else None
        if not isinstance(status, dict) or status.get("type") != "status":
            return result

        self._cache_fxtwitter_status(status)
        return self._adapt_fxtwitter_status(status, username, tweet_id)

    async def get_tweet(self, username: str, tweet_id: str) -> dict:
        """获取推文详细信息

        返回:
            推文信息字典，包含 text, images, videos, quote, is_r18,
            screen_name, retweet 等
        """
        if self.provider == DATA_PROVIDER_FXTWITTER:
            return await self._get_fxtwitter_tweet(username, tweet_id)

        result = self._empty_tweet_result(username, tweet_id)

        if not self.nitter_url:
            return result

        client = await self._get_client()
        nitter_url = f"{self.nitter_url}/{username}/status/{tweet_id}"

        try:
            resp = await client.get(nitter_url, timeout=20.0)
            if resp.status_code != 200:
                logger.warning(f"获取推文失败: {nitter_url}, 状态码: {resp.status_code}")
                return result

            soup = BeautifulSoup(resp.text, "html.parser")

            # 限定在主贴容器内，避免匹配评论/回复区内容
            # Nitter 推文详情页：主贴在 div.main-tweet 内，评论在其后
            main_tweet = soup.select_one("div.main-tweet")
            if not main_tweet:
                logger.warning(f"未找到 div.main-tweet 容器: {nitter_url}")
                return result

            result["status"] = True

            # 获取显示名称
            fullname_elem = main_tweet.select_one("a.fullname")
            if fullname_elem:
                result["screen_name"] = fullname_elem.get_text(strip=True)

            username_elem = main_tweet.select_one("a.username")
            if username_elem:
                result["username"] = username_elem.get_text(strip=True).lstrip("@")

            result["avatar"] = self._extract_avatar(main_tweet)
            result["verified"] = self._has_verified_badge(main_tweet)
            result["date"] = self._extract_date(main_tweet)
            result["stats"] = self._extract_stats(main_tweet)

            # 获取推文正文
            content_elem = main_tweet.select_one("div.tweet-content.media-body")
            if content_elem:
                result["text"] = content_elem.get_text(strip=True)

            # 获取图片（仅主贴，排除视频/GIF缩略图）
            result["images"] = self._extract_images(main_tweet)

            # 获取视频/GIF（仅主贴）
            # Nitter 视频有三种HTML形态：
            #   1) mp4播放启用: <video><source src=""></video>
            #   2) m3u8/vmap格式: <video data-url=""> (无src/source)
            #   3) 播放被禁用: 仅有 <img> 缩略图 + <div class="video-overlay">
            result["videos"] = self._extract_videos(main_tweet)
            result["video_previews"] = self._extract_video_previews(main_tweet)

            # 检测直播推文并过滤
            is_live_stream = self._contains_live_stream(main_tweet)

            if is_live_stream:
                logger.info(
                    f"检测到直播/流媒体视频 @{username}/{tweet_id}，"
                    f"过滤所有媒体内容"
                )
                result["videos"] = []
                result["images"] = []
                result["video_previews"] = []

            # 检测视频附件但未提取到视频URL的情况
            if not is_live_stream:
                video_overlays = main_tweet.select("div.video-overlay")
                if video_overlays and not result["videos"]:
                    logger.warning(
                        f"检测到视频附件但未提取到视频URL，"
                        f"可能 Nitter 实例({self.nitter_url})禁用了视频播放。"
                        f"请在 Nitter 配置中设置 hlsPlayback = true 且 proxyVideo = false"
                    )

            # 获取引用推文（仅主贴）
            quote_elem = main_tweet.select_one("div.quote")
            if quote_elem:
                quote_text_elem = quote_elem.select_one(
                    "div.quote-text, div.tweet-content"
                )
                quote_author = quote_elem.select_one("a.fullname")
                quote_username = quote_elem.select_one("a.username")
                quote_link = quote_elem.select_one("a.quote-link")
                quote_href = quote_link.get("href", "") if quote_link else ""
                quote_id_match = re.search(r"/status/(\d+)", quote_href)
                quote_live_stream = self._contains_live_stream(
                    quote_elem,
                    include_nested_quotes=True,
                )
                result["quote"] = {
                    "author": quote_author.get_text(strip=True) if quote_author else "",
                    "username": (
                        quote_username.get_text(strip=True).lstrip("@")
                        if quote_username
                        else ""
                    ),
                    "avatar": self._extract_avatar(quote_elem),
                    "verified": self._has_verified_badge(
                        quote_elem,
                        include_nested_quotes=True,
                    ),
                    "date": self._extract_date(quote_elem),
                    "tweet_id": quote_id_match.group(1) if quote_id_match else "",
                    "text": quote_text_elem.get_text(strip=True) if quote_text_elem else "",
                    "images": (
                        []
                        if quote_live_stream
                        else self._extract_images(
                            quote_elem,
                            include_nested_quotes=True,
                        )
                    ),
                    "videos": (
                        []
                        if quote_live_stream
                        else self._extract_videos(
                            quote_elem,
                            include_nested_quotes=True,
                        )
                    ),
                    "video_previews": (
                        []
                        if quote_live_stream
                        else self._extract_video_previews(
                            quote_elem,
                            include_nested_quotes=True,
                        )
                    ),
                }

            # 检测 R18 标记（仅主贴）
            r18_elem = main_tweet.select_one(".nsfw")
            result["is_r18"] = r18_elem is not None

        except Exception as e:
            logger.error(f"获取推文详情失败 {username}/{tweet_id}: {e}")

        return result

def get_next_website(website_list: list[str], current: str) -> Optional[str]:
    """获取列表中当前镜像站的下一个（循环）"""
    if not website_list:
        return None
    try:
        idx = website_list.index(current)
        return website_list[(idx + 1) % len(website_list)]
    except ValueError:
        return website_list[0]
