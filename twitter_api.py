"""
Twitter API 交互模块
通过 Nitter 镜像站获取 Twitter/X 推文数据
"""

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag
from astrbot.api import logger

# 内置 Nitter 镜像站列表
WEBSITE_LIST = [
    "https://nitter.net",
]

# 有效的图片质量选项
IMAGE_QUALITY_OPTIONS = ("large", "orig")

# 直播推文链接特征（推文链接中包含此路径即为直播）
BROADCAST_LINK_PATTERN = re.compile(r'/i/broadcasts/', re.IGNORECASE)


class TwitterAPI:
    """Twitter API 交互类，通过 Nitter 镜像站获取推文"""

    def __init__(self, proxy: Optional[str] = None, nitter_url: str = "",
                 image_quality: str = "orig"):
        self.proxy = proxy
        self.nitter_url = nitter_url
        self.image_quality = image_quality if image_quality in IMAGE_QUALITY_OPTIONS else "orig"
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            proxy = self.proxy if self.proxy else None
            self._client = httpx.AsyncClient(
                proxy=proxy,
                http2=True,
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
        return self._client

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
        client = await self._get_client()
        for url in website_list:
            try:
                test_url = f"{url}/elonmusk"
                resp = await client.get(test_url, timeout=15.0)
                if resp.status_code == 200:
                    logger.info(f"Nitter 镜像站可用: {url}")
                    self.nitter_url = url
                    return url
                logger.debug(f"Nitter 镜像站不可用: {url}, 状态码: {resp.status_code}")
            except Exception as e:
                logger.debug(f"Nitter 镜像站检测异常: {url}, 错误: {e}")
                continue
        logger.warning("所有 Nitter 镜像站均不可用")
        return None

    async def get_user_info(self, username: str) -> dict:
        """获取 Twitter 用户信息

        返回:
            {"status": bool, "screen_name": str, "bio": str, "user_name": str}
        """
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

    async def get_user_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0
    ) -> list[dict]:
        """获取用户时间线条目，包含转帖元数据。"""
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

    async def get_tweet(self, username: str, tweet_id: str) -> dict:
        """获取推文详细信息

        返回:
            推文信息字典，包含 text, images, videos, quote, is_r18,
            screen_name, retweet 等
        """
        result = {
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
        }

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
