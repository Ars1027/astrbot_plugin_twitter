"""Nitter 请求与解析；方法使用 TwitterAPI 实例的共享状态。"""

import re
from typing import Optional
from urllib.parse import urlsplit

from astrbot.api import logger
from bs4 import BeautifulSoup, Tag

from ..twitter_article import attach_article, parse_nitter_article
from . import DATA_PROVIDER_NITTER, TwitterTimelineError

WEBSITE_LIST = [
    "https://nitter.net",
]

NITTER_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BROADCAST_LINK_PATTERN = re.compile(r'/i/broadcasts/', re.IGNORECASE)


class NitterProviderMixin:
    """内部方法分组；配置、HTTP 客户端和缓存由 TwitterAPI 管理。"""

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

    def _extract_nitter_media(
        self, container: Tag, include_nested_quotes: bool = False
    ) -> dict:
        """附加媒体分别降级，单项解析失败不丢弃正文或其他媒体。"""
        media = {"images": [], "videos": [], "video_previews": []}
        for key, extract in (
            ("images", self._extract_images),
            ("videos", self._extract_videos),
            ("video_previews", self._extract_video_previews),
        ):
            try:
                media[key] = extract(container, include_nested_quotes=include_nested_quotes)
            except Exception as exc:
                logger.warning(f"解析 Nitter {key} 失败，跳过该附加媒体: {exc}")
        return media

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

    async def _attach_nitter_article(
        self, tweet: dict, container: Tag, tweet_id: str,
        include_nested_quotes: bool = False,
    ) -> None:
        card = next((c for c in container.select(".article-card")
                     if include_nested_quotes or not self._is_nested_quote_element(c, container)), None)
        if not card:
            return
        title = card.select_one(".card-title")
        preview = card.select_one(".card-description")
        cover = card.select_one("img")

        def resolve_image(img: Tag) -> str:
            parent = img.find_parent("a")
            href = str(parent.get("href", "")) if parent else ""
            parts = urlsplit(href)
            is_image_link = parts.path.startswith("/pic/") or bool(
                re.search(r"\.(?:jpe?g|png|gif|webp)(?::orig)?$", parts.path, re.I)
            )
            src = str(img.get("src", ""))
            return self._absolute_url(self._build_image_url(
                href if is_image_link else src, src,
            ))

        article = {
            "title": title.get_text(strip=True) if title else "",
            "preview_text": preview.get_text(" ", strip=True) if preview else "",
            "cover": resolve_image(cover) if cover else "", "blocks": [],
            "url": f"https://x.com/{tweet.get('username')}/status/{tweet_id}",
        }
        # Nitter 的文章入口使用推文 ID，而非 X article ID；请求固定为当前实例。
        if str(tweet_id).isdigit():
            try:
                client = await self._get_client()
                response = await client.get(f"{self.nitter_url}/i/article/{tweet_id}", timeout=20.0)
                if response.status_code == 200:
                    article = parse_nitter_article(response.text, article, resolve_image, self._absolute_url)
            except Exception as exc:
                logger.warning(f"获取 Nitter 文章正文失败，使用摘要: {type(exc).__name__}")
        attach_article(tweet, article)

    async def _get_nitter_user_info(self, username: str) -> dict:
        """执行 Nitter 数据源请求，使用 TwitterAPI 的共享状态。"""
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

    async def _get_nitter_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0,
    ) -> list[dict]:
        """执行 Nitter 数据源请求，使用 TwitterAPI 的共享状态。"""
        if not self.nitter_url:
            return []

        url = f"{self.nitter_url}/{username}"
        try:
            client = await self._get_client()
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                raise TwitterTimelineError(
                    f"获取 @{username} 的 Nitter 时间线失败：HTTP {resp.status_code}"
                )

            soup = BeautifulSoup(resp.text, "html.parser")
            return self._parse_timeline_items(
                soup,
                username=username,
                since_id=since_id,
                limit=limit,
            )
        except TwitterTimelineError:
            raise
        except Exception as e:
            raise TwitterTimelineError(
                f"获取 @{username} 的 Nitter 时间线失败：{type(e).__name__}"
            ) from e

    async def _get_nitter_tweet(self, username: str, tweet_id: str) -> dict:
        """执行 Nitter 数据源请求，使用 TwitterAPI 的共享状态。"""
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

            # 获取主贴附加媒体，排除嵌套引用帖；各类媒体独立降级。
            result.update(self._extract_nitter_media(main_tweet))

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
                    **({"images": [], "videos": [], "video_previews": []}
                       if quote_live_stream else self._extract_nitter_media(
                           quote_elem, include_nested_quotes=True,
                       )),
                }

            # 主贴与引用帖分别跟进文章页，禁止使用页面中其他推文的卡片。
            for part, container, nested in (
                (result, main_tweet, False), (result.get("quote"), quote_elem, True),
            ):
                if part is not None and container is not None:
                    try:
                        await self._attach_nitter_article(
                            part, container, part["tweet_id"], include_nested_quotes=nested,
                        )
                    except Exception as exc:
                        logger.warning(f"解析 Nitter 文章失败，保留已有正文: {exc}")

            # 检测 R18 标记（仅主贴）
            r18_elem = main_tweet.select_one(".nsfw")
            result["is_r18"] = r18_elem is not None
            result["status"] = True

        except Exception as e:
            logger.error(f"获取推文详情失败 {username}/{tweet_id}: {e}")

        return result
