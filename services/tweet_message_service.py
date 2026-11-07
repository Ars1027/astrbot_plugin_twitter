"""推文翻译、文本排版、截图渲染和媒体组件构建。"""

import asyncio
import copy
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

from ..twitter_renderer import (
    build_tweet_card_context,
    load_tweet_card_template,
    tweet_card_render_options,
    tweet_card_page_contexts,
)
from .avatar_cache_service import AvatarCacheService
from ..twitter_diagnostics import anchor, detail_debug, exception_type_chain, reference
from ..twitter_article import article_text


HtmlRender = Callable[..., Awaitable[str]]


class TweetMessageChain(list):
    """保留正文截图身份；其中全部图片送达才可确认整条推文。"""

    def __init__(self, components=(), *, required_images=()):
        super().__init__(components)
        self.required_images = tuple(required_images)


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
    translate_timeout_seconds: int = 60
    translate_method: str = "llm"
    translate_native_lang: str = "zh"


@dataclass(slots=True)
class TranslationCycleState:
    """仅在一轮轮询内共享的 Provider 失败计数，手动解析不使用。"""

    failures: dict[str, int] = field(default_factory=dict)
    skipped: int = 0

    def should_skip(self, provider_id: str) -> bool:
        if self.failures.get(provider_id, 0) < 2:
            return False
        self.skipped += 1
        return True

    def record(self, provider_id: str, failed: bool) -> None:
        self.failures[provider_id] = (
            self.failures.get(provider_id, 0) + 1 if failed else 0
        )
        if self.failures[provider_id] == 2:
            logger.warning(
                f"翻译 Provider {provider_id} 连续两条推文失败，"
                "本轮剩余推文使用原文，下轮重新尝试"
            )


class TweetMessageService:
    """生成普通文本或 X 风格截图推文消息链。"""

    def __init__(
        self,
        context: Any,
        twitter_api: Any,
        html_render: HtmlRender,
        settings: TweetMessageSettings,
        avatar_cache: AvatarCacheService | None = None,
    ) -> None:
        self.context = context
        self.twitter_api = twitter_api
        self.html_render = html_render
        self.settings = settings
        self.avatar_cache = avatar_cache

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
        *,
        video_max_size_mb: int | None = None,
    ) -> str:
        """构建超限视频降级为链接时展示给用户的文本。"""
        size_note = ""
        if size_bytes:
            size_mb = size_bytes / 1024 / 1024
            size_note = f"（约 {size_mb:.1f} MB）"
        limit = self.settings.video_max_size_mb if video_max_size_mb is None else video_max_size_mb
        return (
            f"\n视频大小超过 {limit} MB{size_note}，"
            f"已改为发送链接：{video_url}"
        )

    async def video_exceeds_size_limit(
        self,
        video_url: str,
        *,
        video_max_size_mb: int | None = None,
    ) -> tuple[bool, int | None]:
        """尽量检查视频大小；未知大小和流媒体 URL 默认放行。"""
        if self.is_stream_video_url(video_url):
            return False, None

        size_bytes = await self.twitter_api.get_remote_file_size(video_url)
        if size_bytes is None:
            return False, None

        limit = self.settings.video_max_size_mb if video_max_size_mb is None else video_max_size_mb
        limit_bytes = limit * 1024 * 1024
        return size_bytes > limit_bytes, size_bytes

    async def append_media_components(
        self,
        chain: list,
        images: list,
        videos: list,
        context_label: str = "推文",
        *,
        video_max_size_mb: int | None = None,
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
                    video_url, video_max_size_mb=video_max_size_mb,
                )
                detail_debug(
                    logger, self,
                    "video.size", media=reference(video_url), bytes=size_bytes,
                    limit_mb=self.settings.video_max_size_mb if video_max_size_mb is None else video_max_size_mb,
                    exceeded=exceeds_limit, source=context_label,
                    stream=self.is_stream_video_url(video_url),
                )
                if exceeds_limit:
                    logger.warning(
                        f"{context_label}视频超过大小限制，已改为链接: {video_url}"
                    )
                    chain.append(
                        Comp.Plain(
                            str(self.video_limit_message(
                                video_url, size_bytes, video_max_size_mb=video_max_size_mb,
                            ))
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
            started = perf_counter()
            detail_debug(logger, self, "image.download.start", media=reference(img_url), proxy=True)
            data = await self.twitter_api.download_media(img_url)
        except Exception as exc:
            logger.debug(anchor(
                "image.download.failed", media=reference(img_url), causes=exception_type_chain(exc),
                elapsed=round(perf_counter() - started, 2),
            ))
            logger.warning(
                f"通过代理下载图片失败 {img_url}: {exc}，回退为远程 URL"
            )
            return Comp.Image.fromURL(img_url)

        detail_debug(
            logger, self,
            "image.download.complete", media=reference(img_url), bytes=len(data),
            elapsed=round(perf_counter() - started, 2),
        )
        return Comp.Image.fromBytes(data)

    async def maybe_translate(
        self,
        tweet_info: dict,
        umo: str,
        cycle: TranslationCycleState | None = None,
    ) -> tuple[str | None, str | None]:
        """正文、引用和重试共用总时限，保留超时前已完成的译文。"""
        quote = tweet_info.get("quote") or {}
        quote.pop("translated_text", None)
        for part in (tweet_info, quote):
            part.pop("translated_article", None)
            part.pop("translated_article_lead", None)
        if not self.settings.translate_enabled:
            return None, None

        if self.settings.translate_method == "x":
            return await self._translate_native(tweet_info)

        original_text = str(tweet_info.get("text") or "")
        quote_text = str(quote.get("text") or "")
        if not (original_text.strip() or quote_text.strip()):
            return None, None

        translated_text: str | None = None
        translate_model: str | None = None
        provider_id: str | None = None
        failed = False
        skipped = False
        started = asyncio.get_running_loop().time()

        async def translate_parts() -> None:
            nonlocal provider_id, translated_text, translate_model, failed, skipped
            provider_id = await self.get_translate_provider_id(umo)
            if not provider_id:
                return
            if cycle is not None and cycle.should_skip(provider_id):
                skipped = True
                return
            if self._has_translatable_text(original_text):
                main_translated, main_model = await self._translate_tweet_part(tweet_info, provider_id)
                if main_model:
                    translated_text, translate_model = main_translated, main_model
                else:
                    failed = True
            if self._has_translatable_text(quote_text):
                quote_translated, quote_model = await self._translate_tweet_part(quote, provider_id)
                if quote_model:
                    quote["translated_text"] = quote_translated
                    translate_model = translate_model or quote_model
                else:
                    failed = True

        try:
            await asyncio.wait_for(
                translate_parts(), timeout=self.settings.translate_timeout_seconds
            )
        except asyncio.TimeoutError:
            failed = True
            logger.warning(
                f"推文翻译超过总时限 {self.settings.translate_timeout_seconds} 秒，"
                f"未完成部分使用原文，Provider: {provider_id or '选择中'}"
            )
        except Exception as exc:
            failed = True
            logger.warning(f"推文翻译异常，未完成部分使用原文: {type(exc).__name__}")

        if cycle is not None and provider_id and not skipped:
            cycle.record(provider_id, failed)
        elapsed = asyncio.get_running_loop().time() - started
        logger.debug(
            f"推文翻译处理耗时 {elapsed:.2f} 秒，Provider: {provider_id or '无'}，"
            f"失败回退: {failed}，本轮跳过: {skipped}"
        )
        return translated_text, translate_model

    async def _translate_native(self, tweet: dict) -> tuple[str | None, str | None]:
        """X 翻译仅用于 FxTwitter；不可用时保留原文，不调用 LLM。"""
        if self.twitter_api.provider != "fxtwitter":
            return None, None
        try:
            result = await asyncio.wait_for(
                self.twitter_api.get_tweet_translation(
                    tweet.get("username", ""), tweet.get("tweet_id", ""),
                    self.settings.translate_native_lang,
                ),
                timeout=self.settings.translate_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"X 翻译失败，使用原文: {type(exc).__name__}")
            return None, None
        if not result:
            return None, None
        translated = result.get("text") or None
        quote_text = (result.get("quote") or {}).get("text")
        if quote_text and tweet.get("quote"):
            tweet["quote"]["translated_text"] = quote_text
        for part, text in ((tweet, translated), (tweet.get("quote") or {}, quote_text)):
            if text and part.get("article"):
                # 接口只翻译帖子正文，文章内容仍保留原文。
                part["translated_article"] = copy.deepcopy(part["article"])
                part["translated_article_lead"] = text
        return translated, "X" if translated or quote_text else None

    @staticmethod
    def _has_translatable_text(text: str) -> bool:
        return bool(re.sub(r"https?://\S+", "", text).strip())

    async def _translate_tweet_part(self, tweet: dict, provider_id: str) -> tuple[str, str | None]:
        article = tweet.get("article")
        if not article:
            return await self._translate_with_provider(str(tweet.get("text") or ""), provider_id)
        blocks = article.get("blocks") or []
        units = [tweet.get("article_lead") or "", article.get("title") or "",
                 article.get("preview_text") or ""] + [b.get("text") or "" for b in blocks]
        indexes = [i for i, text in enumerate(units) if self._has_translatable_text(text)]
        if not indexes:
            return tweet["text"], None
        translated, model = await self._translate_with_provider(
            json.dumps([units[i] for i in indexes], ensure_ascii=False), provider_id,
            structured=True,
        )
        if not model:
            return tweet["text"], None
        try:
            result = json.loads(translated)
            if (not isinstance(result, list) or len(result) != len(indexes)
                    or any(not isinstance(s, str) or not s.strip() for s in result)):
                raise ValueError("article translation shape mismatch")
        except (ValueError, TypeError):
            logger.warning("文章翻译未保持段落结构，使用原文")
            return tweet["text"], None
        for index, text in zip(indexes, result):
            units[index] = text
        translated_article = copy.deepcopy(article)
        translated_article["title"], translated_article["preview_text"] = units[1:3]
        for block, text in zip(translated_article["blocks"], units[3:]):
            block["text"] = text
            # 翻译后字符偏移不再有效；保留整段样式与列表层级。
            old_spans = block.get("spans") or []
            attrs = {k: v for k, v in old_spans[0].items() if k != "text"} if len(old_spans) == 1 else {}
            block["spans"] = [{"text": text, **attrs}]
        tweet["translated_article"] = translated_article
        tweet["translated_article_lead"] = units[0]
        return article_text(
            translated_article, units[0], include_link=self.settings.include_tweet_link,
        ), model

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
        """单段翻译也复用总时限保护，失败时返回原文。"""
        translated, model = await self.maybe_translate({"text": text}, umo)
        return translated if translated is not None else text, model

    async def _translate_with_provider(
        self, text: str, provider_id: str, structured: bool = False,
    ) -> tuple[str, str | None]:
        """在调用方的总时限内翻译一段文本，不拦截任务取消。"""
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

        if structured:
            system_prompt += (
                "\n输入是 JSON 字符串数组。逐项翻译，保持数组长度与顺序，空字符串保持为空。"
                "只输出合法 JSON 字符串数组，不使用 Markdown 代码块，不合并、删减或添加段落。"
                "保留链接，不访问外部网页。"
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
                logger.warning(
                    f"翻译失败 (尝试 {attempt + 1}/{max_retries}): "
                    f"{type(exc).__name__}"
                )

            if attempt < max_retries - 1:
                await asyncio.sleep(1)

        logger.warning("翻译全部重试失败，使用原文")
        return text, None

    def _display_text(self, tweet: dict, translated_text: str | None = None) -> str:
        article = tweet.get("article")
        if article and (translated_text is None or tweet.get("translated_article")):
            if translated_text is not None:
                article = tweet["translated_article"]
                lead = tweet.get("translated_article_lead") or ""
            else:
                lead = tweet.get("article_lead") or ""
            return article_text(article, lead, include_link=self.settings.include_tweet_link)
        return str(translated_text or tweet.get("text") or "")

    async def build_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
        *,
        video_max_size_mb: int | None = None,
    ) -> list:
        """构建纯文本模式下的推文消息链。"""
        if sub_config is None:
            sub_config = {"r18": True, "media": False, "status": True}

        text = self._display_text(tweet_info, translated_text)
        quote = tweet_info.get("quote")
        tweet_id = str(tweet_info.get("tweet_id") or "")
        author_username = str(tweet_info.get("username") or username)
        screen_name = str(tweet_info.get("screen_name") or author_username)
        retweet = tweet_info.get("retweet") or None

        chain = []

        def append_text_section(value: str) -> None:
            value = str(value or "").strip()
            if value:
                separator = "\n\n" if chain else ""
                self.append_to_last_plain(chain, separator + value)

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

        await self.append_media_components(
            chain, tweet_info.get("images") or [], tweet_info.get("videos") or [],
            video_max_size_mb=video_max_size_mb,
        )

        if quote:
            quote_author_username = str(quote.get("username") or "")
            quote_author = str(quote.get("author") or quote_author_username)
            quote_text = self._display_text(quote, quote.get("translated_text"))
            quote_display = self.build_author_display(
                quote_author_username,
                quote_author,
            )
            append_text_section(f"{nickname} 引用了 {quote_display} 的帖子")
            if quote_text:
                append_text_section(quote_text)
            await self.append_media_components(
                chain, quote.get("images") or [], quote.get("videos") or [],
                context_label="引用推文",
                video_max_size_mb=video_max_size_mb,
            )

        if tweet_id and self.settings.include_tweet_link:
            append_text_section(
                f"https://x.com/{author_username}/status/{tweet_id}"
            )

        quote_translated = bool((quote or {}).get("translated_text"))
        if translate_model and (translated_text is not None or quote_translated):
            append_text_section(f"（由 {translate_model} 翻译自原文）")

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
        *,
        video_max_size_mb: int | None = None,
    ) -> list:
        """按当前文本渲染模式构建推文消息链。"""
        if self.settings.text_render_mode != "screenshot":
            return await self.build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
                video_max_size_mb=video_max_size_mb,
            )

        try:
            return await self.build_screenshot_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
                video_max_size_mb=video_max_size_mb,
            )
        except Exception as exc:
            logger.warning(
                f"推文截图渲染失败，已回退为文本消息: {exception_type_chain(exc)}; "
                f"{anchor('render.fallback', author=username, tweet=str(tweet_info.get('tweet_id') or ''))}"
            )
            return await self.build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
                video_max_size_mb=video_max_size_mb,
            )

    async def build_screenshot_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
        *,
        video_max_size_mb: int | None = None,
    ) -> list:
        """构建正文以 X 风格卡片截图展示的消息链。"""
        if sub_config is None:
            sub_config = {"r18": True, "media": False, "status": True}

        chain: list = []
        required_images: list = []
        has_media = self.tweet_has_media(tweet_info)
        render_text_card = not (self.settings.no_text and has_media)

        if render_text_card:
            render_tweet_info = await self.prepare_screenshot_media(tweet_info)

            context = build_tweet_card_context(
                username,
                render_tweet_info,
                translated_text=translated_text,
                translate_model=translate_model,
                theme=self.settings.screenshot_theme,
                include_tweet_link=self.settings.include_tweet_link,
            )
            pages = tweet_card_page_contexts(context)
            for page_number, page_context in enumerate(pages, 1):
                started = perf_counter()
                detail_debug(
                    logger, self,
                    "render.start", author=username, tweet=str(tweet_info.get("tweet_id") or ""),
                    page=page_number, pages=len(pages),
                )
                rendered_url = await self.html_render(
                    load_tweet_card_template(), page_context,
                    options=tweet_card_render_options(page_context),
                )
                image_comp = self.rendered_image_component(rendered_url)
                if image_comp is None:
                    raise RuntimeError("html_render returned an empty image result")
                chain.append(image_comp)
                required_images.append(image_comp)
                detail_debug(
                    logger, self,
                    "render.complete", author=username, tweet=str(tweet_info.get("tweet_id") or ""),
                    page=page_number, elapsed=round(perf_counter() - started, 2),
                )

        link_comp = self.tweet_link_component(tweet_info, username)
        if link_comp is not None:
            # TG 逐组件发送，独立的换行会成为空消息并触发截图降级重发。
            chain.append(link_comp)

        await self.append_tweet_media(chain, tweet_info, video_max_size_mb=video_max_size_mb)

        return TweetMessageChain(
            [component for component in chain if component is not None],
            required_images=required_images,
        )

    async def append_tweet_media(
        self, chain: list, tweet_info: dict, *, video_max_size_mb: int | None = None,
    ) -> None:
        """按主帖、引用帖的顺序追加截图外的附加媒体。"""
        for part, label in ((tweet_info, "推文"), (tweet_info.get("quote"), "引用推文")):
            if part:
                await self.append_media_components(
                    chain, part.get("images") or [], part.get("videos") or [],
                    context_label=label,
                    video_max_size_mb=video_max_size_mb,
                )

    async def prepare_screenshot_media(self, tweet_info: dict) -> dict:
        """头像始终走缓存，其他截图媒体仍按原代理预下载开关处理。"""
        result = copy.deepcopy(tweet_info)
        for author in (result, result.get("quote") or {}):
            author["avatar"] = (
                await self.avatar_cache.get(author.get("avatar"))
                if self.avatar_cache is not None
                else None
            )
        if not (self.settings.pre_download_media and self.settings.proxy):
            return result

        for part in (result, result.get("quote") or {}):
            part["images"] = [
                await self.download_to_data_uri_safe(str(url)) or str(url)
                for url in (part.get("images") or [])
            ]

            for preview in part.get("video_previews") or []:
                if isinstance(preview, dict):
                    poster = str(preview.get("poster") or "").strip()
                    if poster:
                        data_uri = await self.download_to_data_uri_safe(poster)
                        if data_uri:
                            preview["poster"] = data_uri

            for key in ("article", "translated_article"):
                article = part.get(key)
                if not article:
                    continue
                for item, media_field in [(article, "cover")] + [
                    (b, key) for b in article.get("blocks") or []
                    for key in (("url",) if b.get("type") == "image" else ("poster",))
                ]:
                    url = item.get(media_field)
                    if url:
                        item[media_field] = await self.download_to_data_uri_safe(url) or url

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
