"""推文翻译、文本排版、截图渲染和媒体组件构建。"""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

from ..twitter_renderer import (
    build_tweet_card_context,
    load_tweet_card_template,
    tweet_card_render_options,
)


HtmlRender = Callable[..., Awaitable[str]]


@dataclass(frozen=True, slots=True)
class TweetMessageSettings:
    """消息构建过程中不会动态变化的配置。"""

    no_text: bool
    send_media_separately: bool
    include_tweet_link: bool
    text_render_mode: str
    screenshot_theme: str
    video_max_size_mb: int
    translate_enabled: bool
    translate_target_lang: str
    translate_provider_id: str
    translate_custom_prompt_enabled: bool
    translate_custom_prompt: str
    pre_download_media: bool
    proxy: str | None


class TweetMessageService:
    """生成普通文本或 X 风格截图推文消息链。"""

    def __init__(
        self,
        context: Any,
        twitter_api: Any,
        html_render: HtmlRender,
        settings: TweetMessageSettings,
    ) -> None:
        self.context = context
        self.twitter_api = twitter_api
        self.html_render = html_render
        self.settings = settings

    @staticmethod
    def build_nickname(username: str, screen_name: str) -> str:
        """构建推主昵称显示。"""
        nickname = f"@{username}"
        if screen_name and screen_name != username:
            nickname += f" ({screen_name})"
        return nickname

    @classmethod
    def build_author_display(cls, username: str, screen_name: str) -> str:
        """构建推文作者显示，兼容只有昵称或用户名的情况。"""
        username = str(username or "").lstrip("@")
        screen_name = str(screen_name or "")
        if username:
            return cls.build_nickname(username, screen_name or username)
        return screen_name or "未知用户"

    @staticmethod
    def tweet_has_media(tweet_info: dict) -> bool:
        """判断主贴或引用帖是否包含媒体。"""
        if tweet_info.get("images") or tweet_info.get("videos"):
            return True
        quote = tweet_info.get("quote") or {}
        return bool(quote.get("images") or quote.get("videos"))

    @staticmethod
    def is_stream_video_url(video_url: str) -> bool:
        """判断视频 URL 是否为流媒体清单类资源。"""
        url = str(video_url or "").lower()
        return ".m3u8" in url or "vmap" in url

    def video_limit_message(
        self,
        video_url: str,
        size_bytes: int | None = None,
    ) -> str:
        """构建超限视频降级为链接时展示给用户的文本。"""
        size_note = ""
        if size_bytes:
            size_mb = size_bytes / 1024 / 1024
            size_note = f"（约 {size_mb:.1f} MB）"
        return (
            f"\n视频大小超过 {self.settings.video_max_size_mb} MB{size_note}，"
            f"已改为发送链接：{video_url}"
        )

    async def video_exceeds_size_limit(
        self,
        video_url: str,
    ) -> tuple[bool, int | None]:
        """尽量检查视频大小；未知大小和流媒体 URL 默认放行。"""
        if self.is_stream_video_url(video_url):
            return False, None

        size_bytes = await self.twitter_api.get_remote_file_size(video_url)
        if size_bytes is None:
            return False, None

        limit_bytes = self.settings.video_max_size_mb * 1024 * 1024
        return size_bytes > limit_bytes, size_bytes

    async def append_media_components(
        self,
        chain: list,
        images: list,
        videos: list,
        context_label: str = "推文",
    ) -> None:
        """把图片和视频追加到消息链，供主贴和引用帖复用。"""
        if not self.settings.send_media_separately:
            return

        for img_url in images:
            try:
                img_comp = await self.build_image_component(str(img_url))
                if img_comp is not None:
                    chain.append(img_comp)
            except Exception as exc:
                logger.warning(f"添加{context_label}图片失败: {img_url}, {exc}")

        for video in videos:
            video_url = str(video)
            try:
                exceeds_limit, size_bytes = await self.video_exceeds_size_limit(
                    video_url
                )
                if exceeds_limit:
                    logger.warning(
                        f"{context_label}视频超过大小限制，已改为链接: {video_url}"
                    )
                    chain.append(
                        Comp.Plain(
                            str(self.video_limit_message(video_url, size_bytes))
                        )
                    )
                    continue

                video_comp = Comp.Video.fromURL(video_url)
                if video_comp is not None:
                    chain.append(video_comp)
            except Exception as exc:
                logger.warning(
                    f"添加{context_label}视频失败，回退为链接: {video_url}, {exc}"
                )
                chain.append(Comp.Plain(str(f"\n视频: {video_url}")))

    async def build_image_component(self, img_url: str) -> Comp.Image | None:
        """根据代理配置选择合适的图片组件构建方式。"""
        img_url = str(img_url or "").strip()
        if not img_url:
            return None

        if not (
            self.settings.pre_download_media
            and self.settings.proxy
        ):
            return Comp.Image.fromURL(img_url)

        try:
            data = await self.twitter_api.download_media(img_url)
        except Exception as exc:
            logger.warning(
                f"通过代理下载图片失败 {img_url}: {exc}，回退为远程 URL"
            )
            return Comp.Image.fromURL(img_url)

        return Comp.Image.fromBytes(data)

    async def maybe_translate(
        self,
        tweet_info: dict,
        umo: str,
    ) -> tuple[str | None, str | None]:
        """根据配置翻译主推文和引用推文文本。"""
        if not self.settings.translate_enabled:
            return None, None

        original_text = str(tweet_info.get("text") or "")
        quote = tweet_info.get("quote") or {}
        quote_text = str(quote.get("text") or "")

        translated_text: str | None = None
        translate_model: str | None = None

        if original_text.strip():
            main_translated, main_model = await self.translate_text(
                original_text,
                umo,
            )
            if main_model:
                translated_text = main_translated
                translate_model = main_model

        if quote_text.strip():
            quote_translated, quote_model = await self.translate_text(
                quote_text,
                umo,
            )
            if quote_model:
                quote["translated_text"] = quote_translated
                translate_model = translate_model or quote_model

        return translated_text, translate_model

    async def get_translate_provider_id(self, umo: str) -> str | None:
        """按配置、当前会话、首个可用项的顺序选择翻译 Provider。"""
        provider_id = self.settings.translate_provider_id
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                logger.debug(f"翻译使用配置指定的 Provider: {provider_id}")
                return provider_id
            logger.warning(
                f"配置的翻译 Provider '{provider_id}' 不可用，尝试回退"
            )

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if provider_id:
                logger.debug(f"翻译使用当前会话的 Provider: {provider_id}")
                return provider_id
        except Exception as exc:
            logger.warning(f"无法获取会话 Provider ID: {exc}")

        try:
            providers = self.context.get_all_providers()
            if providers:
                provider_id = providers[0].meta().id
                logger.debug(f"翻译使用第一个可用 Provider: {provider_id}")
                return provider_id
        except Exception as exc:
            logger.warning(f"无法获取可用 Provider: {exc}")

        logger.error("翻译功能：未找到任何可用的 LLM Provider")
        return None

    async def translate_text(
        self,
        text: str,
        umo: str,
    ) -> tuple[str, str | None]:
        """使用 AstrBot LLM Provider 翻译文本，失败时返回原文。"""
        if not text or not text.strip():
            return text, None

        provider_id = await self.get_translate_provider_id(umo)
        if not provider_id:
            return text, None

        if (
            self.settings.translate_custom_prompt_enabled
            and self.settings.translate_custom_prompt
        ):
            system_prompt = self.settings.translate_custom_prompt.replace(
                "{target_lang}",
                self.settings.translate_target_lang,
            )
        else:
            system_prompt = (
                "你是一个专业的翻译助手。请将用户提供的文本翻译为"
                f"{self.settings.translate_target_lang}。"
                "规则：仅输出翻译结果，不要添加任何解释、前缀、注释或原文对照。"
                "保持原文的语气和格式（如换行、表情符号等）。"
            )

        max_retries = 2
        for attempt in range(max_retries):
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=text,
                    system_prompt=system_prompt,
                )
                translated = llm_resp.completion_text
                if translated and translated.strip():
                    model_name = provider_id
                    try:
                        provider = self.context.get_provider_by_id(provider_id)
                        if provider and hasattr(provider, "meta"):
                            meta = provider.meta()
                            if meta and hasattr(meta, "model_name"):
                                model_name = meta.model_name or provider_id
                    except Exception:
                        pass
                    return translated.strip(), model_name
                logger.warning(
                    f"翻译返回为空 (尝试 {attempt + 1}/{max_retries})"
                )
            except Exception as exc:
                logger.error(
                    f"翻译失败 (尝试 {attempt + 1}/{max_retries}): {exc}"
                )

            if attempt < max_retries - 1:
                await asyncio.sleep(1)

        logger.warning("翻译全部重试失败，使用原文")
        return text, None

    async def build_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """构建纯文本模式下的推文消息链。"""
        if sub_config is None:
            sub_config = {"r18": True, "media": False, "status": True}

        text = str(translated_text or tweet_info.get("text") or "")
        images = tweet_info.get("images") or []
        quote = tweet_info.get("quote")
        tweet_id = str(tweet_info.get("tweet_id") or "")
        author_username = str(tweet_info.get("username") or username)
        screen_name = str(tweet_info.get("screen_name") or author_username)
        retweet = tweet_info.get("retweet") or None

        chain = []
        text_sections: list[str] = []

        def append_text_section(value: str) -> None:
            value = str(value or "").strip()
            if value:
                text_sections.append(value)

        nickname = self.build_author_display(author_username, screen_name)
        if retweet:
            retweeter_username = str(
                retweet.get("retweeter_username") or username
            )
            retweeter_screen_name = str(
                retweet.get("retweeter_screen_name") or retweeter_username
            )
            retweeter = self.build_author_display(
                retweeter_username,
                retweeter_screen_name,
            )
            append_text_section(f"{retweeter} 转发了 {nickname} 的帖子")
        else:
            append_text_section(nickname)

        has_media = self.tweet_has_media(tweet_info)
        if not (self.settings.no_text and has_media) and text:
            append_text_section(text)

        if quote:
            quote_author_username = str(quote.get("username") or "")
            quote_author = str(quote.get("author") or quote_author_username)
            quote_text = str(
                quote.get("translated_text") or quote.get("text") or ""
            )
            quote_display = self.build_author_display(
                quote_author_username,
                quote_author,
            )
            append_text_section(f"{nickname} 引用了 {quote_display} 的帖子")
            if quote_text:
                append_text_section(quote_text)

        if tweet_id and self.settings.include_tweet_link:
            append_text_section(
                f"https://x.com/{author_username}/status/{tweet_id}"
            )

        quote_translated = bool((quote or {}).get("translated_text"))
        if translate_model and (translated_text is not None or quote_translated):
            append_text_section(f"（由 {translate_model} 翻译自原文）")

        if text_sections:
            self.append_to_last_plain(chain, "\n\n".join(text_sections))

        if quote:
            await self.append_media_components(
                chain,
                quote.get("images") or [],
                quote.get("videos") or [],
                context_label="引用推文",
            )

        await self.append_media_components(
            chain,
            images,
            tweet_info.get("videos") or [],
            context_label="推文",
        )

        return [component for component in chain if component is not None]

    def tweet_link_component(
        self,
        tweet_info: dict,
        fallback_username: str,
    ) -> Comp.Plain | None:
        """构建可选的推文链接组件。"""
        tweet_id = str(tweet_info.get("tweet_id") or "")
        author_username = str(
            tweet_info.get("username") or fallback_username
        )
        if not (tweet_id and self.settings.include_tweet_link):
            return None
        return Comp.Plain(
            str(f"https://x.com/{author_username}/status/{tweet_id}")
        )

    @staticmethod
    def append_to_last_plain(chain: list, text: str) -> None:
        """把连续文本放进同一组件，避免适配器吞掉组件间换行。"""
        if chain and isinstance(chain[-1], Comp.Plain):
            current_text = getattr(chain[-1], "text", None)
            if isinstance(current_text, str):
                chain[-1].text = current_text + text
                return
        chain.append(Comp.Plain(str(text)))

    @staticmethod
    def rendered_image_component(rendered_url: str):
        """把 html_render 的输出转换为图片组件。"""
        rendered_url = str(rendered_url or "").strip()
        if not rendered_url:
            return None
        if rendered_url.startswith(("http://", "https://")):
            return Comp.Image.fromURL(rendered_url)
        return Comp.Image.fromFileSystem(rendered_url)

    async def build_message_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """按当前文本渲染模式构建推文消息链。"""
        if self.settings.text_render_mode != "screenshot":
            return await self.build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )

        try:
            return await self.build_screenshot_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )
        except Exception as exc:
            logger.warning(f"推文截图渲染失败，已回退为文本消息: {exc}")
            return await self.build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )

    async def build_screenshot_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """构建正文以 X 风格卡片截图展示的消息链。"""
        if sub_config is None:
            sub_config = {"r18": True, "media": False, "status": True}

        chain: list = []
        has_media = self.tweet_has_media(tweet_info)
        render_text_card = not (self.settings.no_text and has_media)
        render_tweet_info = tweet_info

        if render_text_card:
            if self.settings.pre_download_media and self.settings.proxy:
                render_tweet_info = await self.prepare_screenshot_media(
                    tweet_info
                )

            context = build_tweet_card_context(
                username,
                render_tweet_info,
                translated_text=translated_text,
                translate_model=translate_model,
                theme=self.settings.screenshot_theme,
            )
            rendered_url = await self.html_render(
                load_tweet_card_template(),
                context,
                options=tweet_card_render_options(context),
            )
            image_comp = self.rendered_image_component(rendered_url)
            if image_comp is None:
                raise RuntimeError("html_render returned an empty image result")
            chain.append(image_comp)

        link_comp = self.tweet_link_component(tweet_info, username)
        if link_comp is not None:
            if chain:
                chain.append(Comp.Plain("\n"))
            chain.append(link_comp)

        quote = tweet_info.get("quote") or None
        if quote:
            await self.append_media_components(
                chain,
                quote.get("images") or [],
                quote.get("videos") or [],
                context_label="引用推文",
            )

        await self.append_media_components(
            chain,
            tweet_info.get("images") or [],
            tweet_info.get("videos") or [],
            context_label="推文",
        )

        return [component for component in chain if component is not None]

    async def prepare_screenshot_media(self, tweet_info: dict) -> dict:
        """把截图所需图片转换为 data URI，并保留失败项的原 URL。"""
        result = copy.deepcopy(tweet_info)

        avatar_url = str(result.get("avatar") or "").strip()
        if avatar_url:
            data_uri = await self.download_to_data_uri_safe(avatar_url)
            if data_uri:
                result["avatar"] = data_uri

        result["images"] = [
            await self.download_to_data_uri_safe(str(url)) or str(url)
            for url in (result.get("images") or [])
        ]

        previews = result.get("video_previews") or []
        for preview in previews:
            if isinstance(preview, dict):
                poster = str(preview.get("poster") or "").strip()
                if poster:
                    data_uri = await self.download_to_data_uri_safe(poster)
                    if data_uri:
                        preview["poster"] = data_uri

        quote = result.get("quote") or None
        if quote:
            quote_avatar = str(quote.get("avatar") or "").strip()
            if quote_avatar:
                data_uri = await self.download_to_data_uri_safe(quote_avatar)
                if data_uri:
                    quote["avatar"] = data_uri

            quote["images"] = [
                await self.download_to_data_uri_safe(str(url)) or str(url)
                for url in (quote.get("images") or [])
            ]

            quote_previews = quote.get("video_previews") or []
            for preview in quote_previews:
                if isinstance(preview, dict):
                    poster = str(preview.get("poster") or "").strip()
                    if poster:
                        data_uri = await self.download_to_data_uri_safe(poster)
                        if data_uri:
                            preview["poster"] = data_uri

        return result

    async def download_to_data_uri_safe(self, url: str) -> str | None:
        """安全地将远程 URL 下载并转为 data URI，失败返回 None。"""
        url = str(url or "").strip()
        if not url:
            return None
        try:
            return await self.twitter_api.download_media_to_data_uri(url)
        except Exception as exc:
            logger.debug(f"预下载截图媒体失败 {url}: {exc}")
            return None
