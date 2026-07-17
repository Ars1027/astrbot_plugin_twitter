"""
AstrBot Twitter 推文转发插件
支持 Nitter 与 FxTwitter API 数据源，以及订阅、定时推送、链接识别、合并转发消息、推文翻译

指令列表:
  /推特关注 <推主id> [r18] [媒体]          - 订阅推主
  /推特批量关注 <推主id1> <推主id2> ... [r18] [媒体]  - 批量订阅推主
  /推特取关 <推主id>                        - 取关推主
  /推特批量取关 <推主id1> <推主id2> ...     - 批量取关推主
  /推特清空订阅                             - 清空所有订阅（仅管理员）
  /推特列表                                 - 查看当前订阅列表
  /推特推送 开启/关闭                       - 开启/关闭推送
  /推特测试 <推主id>                        - 立即获取并推送指定推主最新一条推文

配置项:
  【基础设置】
    Twitter 数据源 (twitter_data_provider)          - nitter / fxtwitter
    FxTwitter API (twitter_fxtwitter_api_base)      - 默认 https://api.fxtwitter.com
    Nitter 镜像站地址 (twitter_nitter_url)        - 留空则自动选择
    代理地址 (twitter_proxy)                      - 如 http://127.0.0.1:7890
    轮询间隔 (twitter_poll_interval)              - 默认 5 分钟
  【消息格式】
    合并转发消息 (twitter_use_node)               - 默认开启
    含媒体时隐藏文字 (twitter_no_text)            - 默认关闭
    单独发送媒体资源 (twitter_send_media_separately) - 默认开启
    图片质量 (twitter_image_quality)              - orig / large
    集体转发 (twitter_collective_forward)         - 默认关闭
    附带帖子链接 (twitter_include_tweet_link)     - 默认开启
  【内容过滤】
    推送转帖 (twitter_include_retweets)           - 默认开启
    转帖去重 (twitter_deduplicate_retweets)       - 默认关闭
    链接识别 (twitter_link_recognition_enabled)   - 默认开启
  【翻译设置】
    翻译开关 (twitter_translate_enabled)          - 默认关闭
    目标语言 (twitter_translate_target_lang)      - 默认简体中文
    LLM Provider (twitter_translate_provider_id)  - 留空自动选择

当消息中包含 twitter.com 或 x.com 的推文链接时，自动解析并发送推文内容。
"""

import asyncio
import re
from dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Node, Nodes
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

from .twitter_api import (
    DATA_PROVIDER_FXTWITTER,
    DATA_PROVIDER_NITTER,
    DATA_PROVIDER_OPTIONS,
    DEFAULT_FXTWITTER_API_BASE,
    FxTwitterTimelineError,
    TwitterAPI,
    WEBSITE_LIST,
    get_next_website,
)
from .twitter_renderer import (
    build_tweet_card_context,
    load_tweet_card_template,
    tweet_card_render_options,
)

# Twitter/X 链接正则
TWITTER_LINK_PATTERN = re.compile(
    r"(https?://(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+))"
)

# KV 存储键名
KV_SUBS_KEY = "twitter_subs"
KV_RETWEET_DEDUP_KEY = "twitter_retweet_dedup_seen"
RETWEET_DEDUP_MAX_ITEMS = 500


@dataclass
class CachedTweet:
    """缓存的推文数据，用于集体转发"""

    username: str
    tweet_info: dict
    sub_config: dict
    nickname: str
    translated_text: str | None = None
    translate_model: str | None = None


class TwitterPlugin(Star):
    """Twitter 推文转发插件主类"""

    def _cfg(self, block: str, key: str, default, *legacy_keys: str):
        """读取分组配置，并兼容旧版顶层扁平配置。"""
        block_config = self.config.get(block, {}) or {}
        if isinstance(block_config, dict):
            val = block_config.get(key)
            if val is not None:
                return val

        for cfg_key in (key, *legacy_keys):
            val = self.config.get(cfg_key)
            if val is not None:
                return val

        return default

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 读取配置
        self.proxy = str(self._cfg("basic", "twitter_proxy", "") or "") or None
        self.data_provider = str(
            self._cfg("basic", "twitter_data_provider", DATA_PROVIDER_NITTER)
            or DATA_PROVIDER_NITTER
        ).strip().lower()
        if self.data_provider not in DATA_PROVIDER_OPTIONS:
            logger.warning(
                f"未知 Twitter 数据源: {self.data_provider}，已回退为 nitter"
            )
            self.data_provider = DATA_PROVIDER_NITTER
        self.fxtwitter_api_base = str(
            self._cfg(
                "basic",
                "twitter_fxtwitter_api_base",
                DEFAULT_FXTWITTER_API_BASE,
            )
            or DEFAULT_FXTWITTER_API_BASE
        ).strip().rstrip("/") or DEFAULT_FXTWITTER_API_BASE
        self.use_node = bool(self._cfg("message_format", "twitter_use_node", True))
        self.no_text = bool(self._cfg("message_format", "twitter_no_text", False))
        self.send_media_separately = bool(
            self._cfg(
                "message_format",
                "twitter_send_media_separately",
                True,
            )
        )
        self.link_recognition_enabled = bool(
            self._cfg(
                "content_filter",
                "twitter_link_recognition_enabled",
                True,
            )
        )
        self.poll_interval = max(
            1, int(self._cfg("basic", "twitter_poll_interval", 5))
        )
        self.collective_forward = bool(
            self._cfg("message_format", "twitter_collective_forward", False)
        )
        self.include_retweets = bool(
            self._cfg("content_filter", "twitter_include_retweets", True)
        )
        self.deduplicate_retweets = bool(
            self._cfg("content_filter", "twitter_deduplicate_retweets", False)
        )
        self.include_tweet_link = bool(
            self._cfg(
                "message_format",
                "twitter_include_tweet_link",
                True,
                "twitter_retweet_include_link",
            )
        )
        self.text_render_mode = str(
            self._cfg(
                "message_format",
                "twitter_text_render_mode",
                "text",
            )
            or "text"
        ).strip().lower()
        if self.text_render_mode not in ("text", "screenshot"):
            logger.warning(
                f"未知推文文本渲染模式: {self.text_render_mode}，已回退为 text"
            )
            self.text_render_mode = "text"
        self.screenshot_theme = str(
            self._cfg(
                "message_format",
                "twitter_screenshot_theme",
                "dark",
            )
            or "dark"
        ).strip().lower()
        if self.screenshot_theme not in ("dark", "light"):
            logger.warning(
                f"未知截图主题: {self.screenshot_theme}，已回退为 dark"
            )
            self.screenshot_theme = "dark"
        self.video_max_size_mb = max(
            1,
            int(
                self._cfg(
                    "message_format",
                    "twitter_video_max_size_mb",
                    256,
                )
            ),
        )
        self.collective_max_authors = max(
            1,
            int(
                self._cfg(
                    "message_format",
                    "twitter_collective_max_authors",
                    5,
                )
            ),
        )
        self.translate_enabled = bool(
            self._cfg("translation", "twitter_translate_enabled", False)
        )
        self.translate_target_lang = str(
            self._cfg(
                "translation",
                "twitter_translate_target_lang",
                "简体中文",
            )
            or "简体中文"
        )
        self.translate_provider_id = str(
            self._cfg("translation", "twitter_translate_provider_id", "") or ""
        ).strip()
        self.translate_custom_prompt_enabled = bool(
            self._cfg(
                "translation",
                "twitter_translate_custom_prompt_enabled",
                False,
            )
        )
        self.translate_custom_prompt = str(
            self._cfg("translation", "twitter_translate_custom_prompt", "") or ""
        ).strip()
        self.custom_nitter_url = str(
            self._cfg("basic", "twitter_nitter_url", "") or ""
        ).strip()
        self.image_quality = str(
            self._cfg("message_format", "twitter_image_quality", "orig") or "orig"
        ).strip()
        self.pre_download_media = bool(
            self._cfg("basic", "twitter_pre_download_media", False)
        )

        # 构建镜像站列表（FxTwitter 模式不会使用或检测这些地址）
        self.website_list: list[str] = []
        if self.data_provider == DATA_PROVIDER_NITTER:
            if self.custom_nitter_url:
                self.website_list.append(self.custom_nitter_url)
            self.website_list.extend(WEBSITE_LIST)

        # 初始化 Twitter API
        self.twitter_api = TwitterAPI(
            proxy=self.proxy,
            nitter_url="",
            image_quality=self.image_quality,
            provider=self.data_provider,
            fxtwitter_api_base=self.fxtwitter_api_base,
        )
        self._provider_ready = False

        # 定时任务句柄
        self._poll_task: asyncio.Task | None = None
        self._running = False

        # 集体转发推文缓存：{umo: [CachedTweet, ...]}
        self._collected_tweets: dict[str, list[CachedTweet]] = {}

    async def initialize(self):
        """插件初始化"""
        logger.info("Twitter 推文转发插件初始化中...")

        # 集体转发模式与合并转发消息的兼容性校验
        if self.collective_forward and not self.use_node:
            logger.warning(
                "集体转发模式已开启但合并转发消息未开启，集体转发功能不会生效。"
                "请同时开启「使用合并转发消息」配置项。"
            )

        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            logger.info("当前使用 Twitter 数据源: FxTwitter API")
            self._provider_ready = await self.twitter_api.check_fxtwitter_available()
            if not self._provider_ready:
                logger.warning("FxTwitter API 健康检查失败，推文轮询功能暂不可用")
        else:
            logger.info("当前使用 Twitter 数据源: Nitter")
            available = await self.twitter_api.check_website_available(
                self.website_list
            )
            self._provider_ready = bool(available)
            if available:
                logger.info(f"当前使用 Nitter 镜像站: {available}")
            else:
                logger.warning("未找到可用 Nitter 镜像站，推文轮询功能暂不可用")

        # 启动定时轮询任务
        if self._provider_ready:
            self._running = True
            self._poll_task = asyncio.create_task(self._poll_tweets())
            logger.info(f"推文轮询已启动，间隔 {self.poll_interval} 分钟")

        logger.info("Twitter 推文转发插件初始化完成")

    async def terminate(self):
        """插件销毁"""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        # 停止前发送缓存的推文，避免丢失
        if self._collected_tweets:
            logger.info("正在发送剩余缓存的推文...")
            await self._flush_collected_tweets()
        await self.twitter_api.close()
        logger.info("Twitter 推文转发插件已停止")

    # ========== 数据管理（KV 存储） ==========

    async def _get_subs(self) -> dict:
        """获取全部订阅数据"""
        return await self.get_kv_data(KV_SUBS_KEY, {})

    async def _save_subs(self, data: dict):
        """保存全部订阅数据"""
        await self.put_kv_data(KV_SUBS_KEY, data)

    async def _get_retweet_dedup_seen(self) -> dict:
        """获取转帖去重记录。"""
        data = await self.get_kv_data(KV_RETWEET_DEDUP_KEY, {})
        return data if isinstance(data, dict) else {}

    async def _save_retweet_dedup_seen(self, data: dict):
        """保存转帖去重记录。"""
        await self.put_kv_data(KV_RETWEET_DEDUP_KEY, data)

    # ========== 工具方法 ==========

    @staticmethod
    def _build_nickname(username: str, screen_name: str) -> str:
        """构建推主昵称显示

        参数:
            username: 推主用户名（如 elonmusk）
            screen_name: 显示昵称（如 Elon Musk）

        返回:
            格式化后的昵称，如 "@elonmusk (Elon Musk)" 或 "@elonmusk"
        """
        nickname = f"@{username}"
        if screen_name and screen_name != username:
            nickname += f" ({screen_name})"
        return nickname

    def _provider_unavailable_message(self) -> str:
        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            return "FxTwitter API 不可用，请检查配置或网络"
        return "Nitter 镜像站不可用，请检查配置或网络"

    @staticmethod
    def _build_author_display(username: str, screen_name: str) -> str:
        """构建推文作者显示，兼容只有昵称或用户名的情况。"""
        username = str(username or "").lstrip("@")
        screen_name = str(screen_name or "")
        if username:
            return TwitterPlugin._build_nickname(username, screen_name or username)
        return screen_name or "未知用户"

    @staticmethod
    def _attach_timeline_item_metadata(tweet_info: dict, item: dict):
        """把时间线条目上的转帖元数据补到推文详情里。"""
        tweet_info["username"] = str(
            item.get("username") or tweet_info.get("username") or ""
        )
        if item.get("is_retweet"):
            tweet_info["retweet"] = {
                "retweeter_username": str(item.get("retweeter_username") or ""),
                "retweeter_screen_name": str(item.get("retweeter_screen_name") or ""),
            }
        else:
            tweet_info["retweet"] = None

    @staticmethod
    def _retweet_seen_by_umo(seen_data: dict, umo: str, tweet_id: str) -> bool:
        """判断某会话是否已经接收过指定原帖的转帖。"""
        seen_ids = seen_data.get(umo) or []
        if not isinstance(seen_ids, list):
            return False
        return str(tweet_id) in {str(item) for item in seen_ids}

    @staticmethod
    def _mark_retweet_seen(seen_data: dict, umo: str, tweet_id: str) -> None:
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

    @staticmethod
    def _tweet_has_media(tweet_info: dict) -> bool:
        """判断主贴或引用帖是否包含媒体。"""
        if tweet_info.get("images") or tweet_info.get("videos"):
            return True
        quote = tweet_info.get("quote") or {}
        return bool(quote.get("images") or quote.get("videos"))

    @staticmethod
    def _is_stream_video_url(video_url: str) -> bool:
        """判断视频 URL 是否为流媒体清单类资源。"""
        url = str(video_url or "").lower()
        return ".m3u8" in url or "vmap" in url

    def _video_limit_message(self, video_url: str, size_bytes: int | None = None) -> str:
        """构建超限视频降级为链接时展示给用户的文本。"""
        size_note = ""
        if size_bytes:
            size_mb = size_bytes / 1024 / 1024
            size_note = f"（约 {size_mb:.1f} MB）"
        return (
            f"\n视频大小超过 {self.video_max_size_mb} MB{size_note}，"
            f"已改为发送链接：{video_url}"
        )

    async def _video_exceeds_size_limit(self, video_url: str) -> tuple[bool, int | None]:
        """尽量检查视频大小；未知大小和流媒体 URL 默认放行。"""
        if self._is_stream_video_url(video_url):
            return False, None

        size_bytes = await self.twitter_api.get_remote_file_size(video_url)
        if size_bytes is None:
            return False, None

        limit_bytes = self.video_max_size_mb * 1024 * 1024
        return size_bytes > limit_bytes, size_bytes

    async def _append_media_components(
        self,
        chain: list,
        images: list,
        videos: list,
        context_label: str = "推文",
    ):
        """把图片和视频追加到消息链，供主贴和引用帖复用。

        当插件配置了代理时，图片会通过代理预下载为内存字节，
        避免下游消息适配器直连外部服务器导致超时。
        """
        if not self.send_media_separately:
            return

        for img_url in images:
            try:
                img_comp = await self._build_image_component(str(img_url))
                if img_comp is not None:
                    chain.append(img_comp)
            except Exception as e:
                logger.warning(f"添加{context_label}图片失败: {img_url}, {e}")

        for v_url in videos:
            video_url = str(v_url)
            try:
                exceeds_limit, size_bytes = await self._video_exceeds_size_limit(video_url)
                if exceeds_limit:
                    logger.warning(
                        f"{context_label}视频超过大小限制，已改为链接: {video_url}"
                    )
                    chain.append(Comp.Plain(str(self._video_limit_message(video_url, size_bytes))))
                    continue

                video_comp = Comp.Video.fromURL(video_url)
                if video_comp is not None:
                    chain.append(video_comp)
            except Exception as e:
                logger.warning(
                    f"添加{context_label}视频失败，回退为链接: {video_url}, {e}"
                )
                chain.append(Comp.Plain(str(f"\n视频: {video_url}")))

    async def _build_image_component(self, img_url: str) -> Comp.Image | None:
        """根据代理配置选择合适的图片组件构建方式。

        有代理时：通过代理下载图片字节，使用 fromBytes，
        避免下游消息适配器直连外部服务器。
        无代理时：使用 fromURL，由 AstrBot 核心/适配器自行下载。
        """
        img_url = str(img_url or "").strip()
        if not img_url:
            return None

        if not (self.pre_download_media and self.proxy):
            return Comp.Image.fromURL(img_url)

        # 通过代理下载并直接构建图片组件，交由 AstrBot 管理媒体数据。
        try:
            data = await self.twitter_api.download_media(img_url)
        except Exception as e:
            logger.warning(f"通过代理下载图片失败 {img_url}: {e}，回退为远程 URL")
            return Comp.Image.fromURL(img_url)

        return Comp.Image.fromBytes(data)

    async def _maybe_translate(
        self, tweet_info: dict, umo: str
    ) -> tuple[str | None, str | None]:
        """根据翻译配置，翻译推文文本和引用推文文本

        参数:
            tweet_info: 推文信息字典
            umo: 会话标识，用于获取 Provider

        返回:
            (主贴翻译后的文本, 翻译模型名称)；引用推文译文写入 quote.translated_text。
        """
        if not self.translate_enabled:
            return None, None

        original_text = str(tweet_info.get("text") or "")
        quote = tweet_info.get("quote") or {}
        quote_text = str(quote.get("text") or "")

        translated_text: str | None = None
        translate_model: str | None = None

        if original_text.strip():
            main_translated, main_model = await self._translate_text(
                original_text, umo
            )
            if main_model:
                translated_text = main_translated
                translate_model = main_model

        if quote_text.strip():
            quote_translated, quote_model = await self._translate_text(
                quote_text, umo
            )
            if quote_model:
                quote["translated_text"] = quote_translated
                translate_model = translate_model or quote_model

        return translated_text, translate_model

    async def _get_translate_provider_id(self, umo: str) -> str | None:
        """获取翻译用的 LLM Provider ID，按优先级回退

        回退顺序：
        1. 配置中指定的 provider_id
        2. 当前会话的 Provider
        3. 第一个可用的 Provider
        """
        # 1. 配置指定的 Provider
        if self.translate_provider_id:
            provider = self.context.get_provider_by_id(self.translate_provider_id)
            if provider:
                logger.debug(f"翻译使用配置指定的 Provider: {self.translate_provider_id}")
                return self.translate_provider_id
            logger.warning(
                f"配置的翻译 Provider '{self.translate_provider_id}' 不可用，尝试回退"
            )

        # 2. 当前会话的 Provider
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if provider_id:
                logger.debug(f"翻译使用当前会话的 Provider: {provider_id}")
                return provider_id
        except Exception as e:
            logger.warning(f"无法获取会话 Provider ID: {e}")

        # 3. 第一个可用的 Provider
        try:
            providers = self.context.get_all_providers()
            if providers:
                provider_id = providers[0].meta().id
                logger.debug(f"翻译使用第一个可用 Provider: {provider_id}")
                return provider_id
        except Exception as e:
            logger.warning(f"无法获取可用 Provider: {e}")

        logger.error("翻译功能：未找到任何可用的 LLM Provider")
        return None

    async def _translate_text(self, text: str, umo: str) -> tuple[str, str | None]:
        """翻译推文文本

        参考 astrbot_plugin_qq_group_daily_analysis 项目的 LLM 调用思路：
        - 使用 system_prompt 分离翻译指令与待翻译内容，提高翻译质量和可靠性
        - 翻译失败时简单重试一次

        参数:
            text: 原始文本
            umo: 订阅者的会话标识，用于获取 Provider

        返回:
            (翻译后的文本, 执行翻译的模型名称)；翻译失败时返回 (原文, None)
        """
        if not text or not text.strip():
            return text, None

        provider_id = await self._get_translate_provider_id(umo)
        if not provider_id:
            return text, None

        if self.translate_custom_prompt_enabled and self.translate_custom_prompt:
            system_prompt = self.translate_custom_prompt.replace(
                "{target_lang}", self.translate_target_lang
            )
        else:
            system_prompt = (
                f"你是一个专业的翻译助手。请将用户提供的文本翻译为{self.translate_target_lang}。"
                f"规则：仅输出翻译结果，不要添加任何解释、前缀、注释或原文对照。"
                f"保持原文的语气和格式（如换行、表情符号等）。"
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
                    # 获取模型名称用于标注
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
                else:
                    logger.warning(f"翻译返回为空 (尝试 {attempt + 1}/{max_retries})")
            except Exception as e:
                logger.error(f"翻译失败 (尝试 {attempt + 1}/{max_retries}): {e}")

            if attempt < max_retries - 1:
                await asyncio.sleep(1)

        logger.warning("翻译全部重试失败，使用原文")
        return text, None

    async def _build_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """构建推文消息链

        参数:
            translated_text: 翻译后的文本，若提供则替换原文
            translate_model: 执行翻译的模型名称，用于末尾标注
        """
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

        # 头部信息
        nickname = self._build_author_display(author_username, screen_name)
        if retweet:
            retweeter_username = str(retweet.get("retweeter_username") or username)
            retweeter_screen_name = str(
                retweet.get("retweeter_screen_name") or retweeter_username
            )
            retweeter = self._build_author_display(
                retweeter_username, retweeter_screen_name
            )
            append_text_section(f"{retweeter} 转发了 {nickname} 的帖子")
        else:
            append_text_section(nickname)

        # 推文正文
        has_media = self._tweet_has_media(tweet_info)
        if not (self.no_text and has_media):
            if text:
                append_text_section(text)

        # 引用推文
        if quote:
            quote_author_username = str(quote.get("username") or "")
            quote_author = str(quote.get("author") or quote_author_username)
            quote_text = str(quote.get("translated_text") or quote.get("text") or "")
            quote_display = self._build_author_display(
                quote_author_username, quote_author
            )
            append_text_section(f"{nickname} 引用了 {quote_display} 的帖子")
            if quote_text:
                append_text_section(quote_text)

        # 推文链接
        if tweet_id and self.include_tweet_link:
            append_text_section(f"https://x.com/{author_username}/status/{tweet_id}")

        # 翻译说明标注
        quote_translated = bool((quote or {}).get("translated_text"))
        if translate_model and (translated_text is not None or quote_translated):
            append_text_section(f"（由 {translate_model} 翻译自原文）")

        if text_sections:
            self._append_to_last_plain(chain, "\n\n".join(text_sections))

        # 引用媒体
        if quote:
            await self._append_media_components(
                chain,
                quote.get("images") or [],
                quote.get("videos") or [],
                context_label="引用推文",
            )

        # 主贴媒体
        await self._append_media_components(
            chain,
            images,
            tweet_info.get("videos") or [],
            context_label="推文",
        )

        # 过滤 None 值，防止类型验证错误
        chain = [c for c in chain if c is not None]
        return chain

    def _tweet_link_component(self, tweet_info: dict, fallback_username: str) -> Comp.Plain | None:
        """构建可选的推文链接组件。"""
        tweet_id = str(tweet_info.get("tweet_id") or "")
        author_username = str(tweet_info.get("username") or fallback_username)
        if not (tweet_id and self.include_tweet_link):
            return None
        return Comp.Plain(str(f"https://x.com/{author_username}/status/{tweet_id}"))

    @staticmethod
    def _append_to_last_plain(chain: list, text: str) -> None:
        """尽量把文本追加到最后一个纯文本组件，避免适配器拼接组件时吞换行。"""
        if chain and isinstance(chain[-1], Comp.Plain):
            current_text = getattr(chain[-1], "text", None)
            if isinstance(current_text, str):
                chain[-1].text = current_text + text
                return
        chain.append(Comp.Plain(str(text)))

    @staticmethod
    def _rendered_image_component(rendered_url: str):
        """把 html_render 的输出转换为图片组件。"""
        rendered_url = str(rendered_url or "").strip()
        if not rendered_url:
            return None
        if rendered_url.startswith(("http://", "https://")):
            return Comp.Image.fromURL(rendered_url)
        return Comp.Image.fromFileSystem(rendered_url)

    async def _build_tweet_message_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """按当前文本渲染模式构建推文消息链。"""
        if self.text_render_mode != "screenshot":
            return await self._build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )

        try:
            return await self._build_screenshot_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )
        except Exception as e:
            logger.warning(f"推文截图渲染失败，已回退为文本消息: {e}")
            return await self._build_tweet_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )

    async def _build_screenshot_tweet_chain(
        self,
        username: str,
        tweet_info: dict,
        sub_config: dict | None = None,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> list:
        """构建正文以 X 风格卡片截图展示的消息链。

        当开启预下载媒体且配置了代理时，提前将所有图片转为 base64
        data URI 内嵌到 HTML 模板中，避免 Chromium 直连外部服务器。
        """
        if sub_config is None:
            sub_config = {"r18": True, "media": False, "status": True}

        chain: list = []
        has_media = self._tweet_has_media(tweet_info)
        render_text_card = not (self.no_text and has_media)
        render_tweet_info = tweet_info

        if render_text_card:
            # 预下载模式：将图片转为 data URI 内嵌，HTML 渲染零外部请求
            if self.pre_download_media and self.proxy:
                render_tweet_info = await self._prepare_screenshot_media(tweet_info)

            context = build_tweet_card_context(
                username,
                render_tweet_info,
                translated_text=translated_text,
                translate_model=translate_model,
                theme=self.screenshot_theme,
            )
            rendered_url = await self.html_render(
                load_tweet_card_template(),
                context,
                options=tweet_card_render_options(context),
            )
            image_comp = self._rendered_image_component(rendered_url)
            if image_comp is None:
                raise RuntimeError("html_render returned an empty image result")
            chain.append(image_comp)

        link_comp = self._tweet_link_component(tweet_info, username)
        if link_comp is not None:
            if chain:
                chain.append(Comp.Plain("\n"))
            chain.append(link_comp)

        quote = tweet_info.get("quote") or None
        if quote:
            await self._append_media_components(
                chain,
                quote.get("images") or [],
                quote.get("videos") or [],
                context_label="引用推文",
            )

        await self._append_media_components(
            chain,
            tweet_info.get("images") or [],
            tweet_info.get("videos") or [],
            context_label="推文",
        )

        return [c for c in chain if c is not None]

    async def _prepare_screenshot_media(self, tweet_info: dict) -> dict:
        """预下载推文中的所有图片并转为 data URI，返回深拷贝后的 tweet_info。

        下载使用插件已配置代理的 TwitterAPI 客户端，确保在网络受限环境
        下也能正常获取图片数据。下载失败时保留原始 URL 作为降级。
        """
        import copy
        result = copy.deepcopy(tweet_info)

        # 主贴头像
        avatar_url = str(result.get("avatar") or "").strip()
        if avatar_url:
            data_uri = await self._download_to_data_uri_safe(avatar_url)
            if data_uri:
                result["avatar"] = data_uri

        # 主贴图片
        result["images"] = [
            await self._download_to_data_uri_safe(str(u)) or str(u)
            for u in (result.get("images") or [])
        ]

        # 视频封面
        previews = result.get("video_previews") or []
        for p in previews:
            if isinstance(p, dict):
                poster = str(p.get("poster") or "").strip()
                if poster:
                    data_uri = await self._download_to_data_uri_safe(poster)
                    if data_uri:
                        p["poster"] = data_uri

        # 引用推文
        quote = result.get("quote") or None
        if quote:
            quote_avatar = str(quote.get("avatar") or "").strip()
            if quote_avatar:
                data_uri = await self._download_to_data_uri_safe(quote_avatar)
                if data_uri:
                    quote["avatar"] = data_uri

            quote["images"] = [
                await self._download_to_data_uri_safe(str(u)) or str(u)
                for u in (quote.get("images") or [])
            ]

            quote_previews = quote.get("video_previews") or []
            for p in quote_previews:
                if isinstance(p, dict):
                    poster = str(p.get("poster") or "").strip()
                    if poster:
                        data_uri = await self._download_to_data_uri_safe(poster)
                        if data_uri:
                            p["poster"] = data_uri

        return result

    async def _download_to_data_uri_safe(self, url: str) -> str | None:
        """安全地将远程 URL 下载并转为 data URI，失败返回 None。"""
        url = str(url or "").strip()
        if not url:
            return None
        try:
            return await self.twitter_api.download_media_to_data_uri(url)
        except Exception as e:
            logger.debug(f"预下载截图媒体失败 {url}: {e}")
            return None

    def _split_chain_for_nodes(
        self, chain: list, nickname: str
    ) -> tuple[list[Node], list[Comp.Video]]:
        """将消息链分离为 Node 列表和待独立发送的视频列表

        视频不能放在 Node 中，否则下载+上传会超出 WebSocket API 超时时间，
        需要作为独立消息发送。

        参数:
            chain: _build_tweet_chain 生成的消息链
            nickname: Node 显示的昵称

        返回:
            (Node 列表, 待独立发送的视频组件列表)
        """
        nodes: list[Node] = []
        video_parts: list[Comp.Video] = []
        text_parts: list = []

        def flush_text_parts():
            nonlocal text_parts
            if text_parts:
                nodes.append(Node(content=text_parts, name=nickname))
                text_parts = []

        for comp in chain:
            if isinstance(comp, Comp.Video):
                video_parts.append(comp)
            elif isinstance(comp, Comp.Image):
                flush_text_parts()
                nodes.append(Node(content=[comp], name=nickname))
            else:
                text_parts.append(comp)

        # 文本节点
        if text_parts:
            nodes.append(Node(content=text_parts, name=nickname))

        return nodes, video_parts

    @staticmethod
    def _build_plain_chain(chain: list) -> list:
        """构建普通消息链，保留图片并把视频转换为链接文本。"""
        plain_chain = []
        for comp in chain:
            if isinstance(comp, Comp.Video):
                vid_url = getattr(comp, "file", "") or getattr(comp, "url", "")
                if vid_url:
                    plain_chain.append(Comp.Plain(str(f"\n视频: {vid_url}")))
            else:
                plain_chain.append(comp)
        return plain_chain

    @staticmethod
    def _split_plain_chain_and_videos(
        chain: list,
    ) -> tuple[list, list[Comp.Video]]:
        """构建普通消息链，并分离需要独立发送的视频组件。"""
        plain_chain = []
        video_parts: list[Comp.Video] = []
        for comp in chain:
            if isinstance(comp, Comp.Video):
                video_parts.append(comp)
            else:
                plain_chain.append(comp)
        return plain_chain, video_parts

    async def _send_plain_chain_resilient(self, umo: str, chain: list) -> None:
        """发送普通消息；媒体导致整链失败时，优先补发文字再逐图尝试。"""
        if not chain:
            return
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))
            return
        except Exception as error:
            logger.warning(f"包含媒体的消息发送失败，尝试保留文字内容: {error}")

        text_parts = [comp for comp in chain if not isinstance(comp, Comp.Image)]
        image_parts = [comp for comp in chain if isinstance(comp, Comp.Image)]

        if text_parts:
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=text_parts)
                )
            except Exception as error:
                logger.error(f"媒体降级后的文字消息仍发送失败: {error}")

        for image_part in image_parts:
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[image_part])
                )
            except Exception as error:
                image_url = getattr(image_part, "file", "") or getattr(
                    image_part, "url", ""
                )
                logger.warning(f"图片发送失败，已保留文字内容: {image_url}, {error}")

    async def _send_video_or_fallback(self, umo: str, vid_comp: Comp.Video):
        """发送视频组件，失败时回退为链接

        参数:
            umo: 目标会话标识
            vid_comp: 视频组件
        """
        try:
            vid_chain = MessageChain(chain=[vid_comp])
            await self.context.send_message(umo, vid_chain)
        except Exception as vid_err:
            logger.warning(f"视频发送失败，回退为链接: {vid_err}")
            vid_url = getattr(vid_comp, "file", "") or getattr(
                vid_comp, "url", ""
            )
            if vid_url:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        chain=[Comp.Plain(str(f"视频: {vid_url}"))]
                    ),
                )

    async def _push_tweet_to_subscribers(
        self, username: str, tweet_info: dict, user_info: dict
    ):
        """将推文推送给所有订阅者（或缓存到集体转发队列）

        注意：每次推送时实时从 KV 存储读取最新订阅数据，
        而非使用轮询开始时的快照，确保订阅状态的变更（取关/新订阅）能即时生效。
        """
        # 实时读取最新订阅数据，避免因订阅状态变更导致的推送错误
        latest_subs = await self._get_subs()
        if username not in latest_subs:
            return  # 该推主已无任何订阅者（可能已被全部取关并删除）
        latest_user_info = latest_subs[username]
        subscribers = latest_user_info.get("subscribers") or {}
        screen_name = str(
            latest_user_info.get("screen_name")
            or tweet_info.get("screen_name")
            or username
        )
        retweet = tweet_info.get("retweet") or {}
        if retweet:
            nickname = self._build_author_display(
                str(retweet.get("retweeter_username") or username),
                str(retweet.get("retweeter_screen_name") or screen_name),
            )
        else:
            nickname = self._build_nickname(username, screen_name)

        should_dedup_retweet = (
            self.deduplicate_retweets
            and bool(retweet)
            and bool(str(tweet_info.get("tweet_id") or ""))
        )
        retweet_dedup_seen: dict | None = None
        retweet_dedup_dirty = False
        if should_dedup_retweet:
            retweet_dedup_seen = await self._get_retweet_dedup_seen()
        retweet_dedup_id = str(tweet_info.get("tweet_id") or "")

        # 翻译推文（如果开启），同一推文只翻译一次
        first_umo = next(iter(subscribers), "")
        translated_text, translate_model = await self._maybe_translate(
            tweet_info, first_umo
        )
        if translate_model:
            original_text = str(tweet_info.get("text") or "")
            quote_text = str((tweet_info.get("quote") or {}).get("text") or "")
            logger.info(
                f"推文翻译完成 @{username}: "
                f"模型={translate_model}, "
                f"原文长度={len(original_text) + len(quote_text)}, "
                f"译文长度={len(translated_text or '')}"
            )

        for umo, sub_config in subscribers.items():
            if not sub_config.get("status", True):
                continue

            # R18 过滤
            is_r18 = tweet_info.get("is_r18", False)
            if is_r18 and not sub_config.get("r18", False):
                continue

            # 媒体过滤
            if sub_config.get("media", False) and not self._tweet_has_media(tweet_info):
                continue

            if should_dedup_retweet and retweet_dedup_seen is not None:
                if self._retweet_seen_by_umo(
                    retweet_dedup_seen,
                    umo,
                    retweet_dedup_id,
                ):
                    logger.debug(
                        f"跳过重复转帖 {umo}: @{username} -> {retweet_dedup_id}"
                    )
                    continue
                self._mark_retweet_seen(
                    retweet_dedup_seen,
                    umo,
                    retweet_dedup_id,
                )
                retweet_dedup_dirty = True

            # 集体转发模式：缓存推文，轮询结束后统一发送
            if self.collective_forward and self.use_node:
                if umo not in self._collected_tweets:
                    self._collected_tweets[umo] = []
                self._collected_tweets[umo].append(
                    CachedTweet(
                        username=username,
                        tweet_info=tweet_info,
                        sub_config=sub_config,
                        nickname=nickname,
                        translated_text=translated_text,
                        translate_model=translate_model,
                    )
                )
                continue

            # 即时推送模式
            await self._send_tweet_to_subscriber(
                umo, username, tweet_info, sub_config, nickname,
                translated_text=translated_text,
                translate_model=translate_model,
            )

        if retweet_dedup_dirty and retweet_dedup_seen is not None:
            await self._save_retweet_dedup_seen(retweet_dedup_seen)

    async def _send_tweet_to_subscriber(
        self,
        umo: str,
        username: str,
        tweet_info: dict,
        sub_config: dict,
        nickname: str,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ):
        """向单个订阅者发送推文消息"""
        try:
            chain = await self._build_tweet_message_chain(
                username, tweet_info, sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                return

            if self.use_node:
                # 合并转发模式：使用 Node/Nodes 构建合并转发消息
                try:
                    nodes, video_parts = self._split_chain_for_nodes(chain, nickname)

                    # 发送合并转发消息（文本+图片）
                    if nodes:
                        message_chain = MessageChain(
                            chain=[Nodes(nodes)]
                        )
                        await self.context.send_message(umo, message_chain)

                    # 视频作为独立消息逐条发送
                    for vid_comp in video_parts:
                        await self._send_video_or_fallback(umo, vid_comp)

                except Exception as node_err:
                    # 合并转发失败，回退到普通消息链（视频改为链接）
                    logger.warning(
                        f"合并转发失败，回退到普通消息: {node_err}"
                    )
                    fallback_chain = self._build_plain_chain(chain)
                    if fallback_chain:
                        await self._send_plain_chain_resilient(
                            umo, fallback_chain
                        )
            else:
                # 普通消息模式：正文和图片先发，视频独立发送，避免混入普通链导致文本异常
                plain_chain, video_parts = self._split_plain_chain_and_videos(chain)
                if plain_chain:
                    await self._send_plain_chain_resilient(umo, plain_chain)
                for vid_comp in video_parts:
                    await self._send_video_or_fallback(umo, vid_comp)

            logger.info(f"推文已推送至 {umo}")
        except Exception as e:
            logger.error(f"推送推文至 {umo} 失败: {e}")

    async def _flush_collected_tweets(self):
        """将缓存的推文按推主分组打包为合并转发消息发送"""
        if not self._collected_tweets:
            return

        collected = self._collected_tweets
        self._collected_tweets = {}

        # 实时读取最新订阅数据，用于校验每个 UMO 是否仍为有效订阅者
        latest_subs = await self._get_subs()

        for umo, cached_list in collected.items():
            if not cached_list:
                continue

            # 校验该 UMO 是否仍是至少一个推主的订阅者
            valid_tweets: list[CachedTweet] = []
            for ct in cached_list:
                user_info = latest_subs.get(ct.username)
                if user_info and umo in user_info.get("subscribers", {}):
                    sub_cfg = user_info["subscribers"][umo]
                    # 检查推送状态
                    if sub_cfg.get("status", True):
                        valid_tweets.append(ct)
                    else:
                        logger.debug(
                            f"集体转发跳过已暂停的订阅: {umo} -> @{ct.username}"
                        )
                else:
                    logger.debug(
                        f"集体转发跳过已取关的订阅: {umo} -> @{ct.username}"
                    )

            if not valid_tweets:
                continue

            try:
                # 按推主分组，保持原始顺序（先到的推主排前面）
                seen_authors: dict[str, list[CachedTweet]] = {}
                author_order: list[str] = []
                for ct in valid_tweets:
                    if ct.username not in seen_authors:
                        seen_authors[ct.username] = []
                        author_order.append(ct.username)
                    seen_authors[ct.username].append(ct)

                # 按最大推主数分批
                max_authors = self.collective_max_authors
                author_batches: list[list[str]] = []
                for i in range(0, len(author_order), max_authors):
                    author_batches.append(author_order[i : i + max_authors])

                for batch_idx, batch_authors in enumerate(author_batches):
                    nodes: list[Node] = []
                    video_queue: list[Comp.Video] = []

                    for author in batch_authors:
                        for ct in seen_authors[author]:
                            chain = await self._build_tweet_message_chain(
                                ct.username, ct.tweet_info, ct.sub_config,
                                translated_text=ct.translated_text,
                                translate_model=ct.translate_model,
                            )
                            if not chain:
                                continue

                            ct_nodes, ct_videos = self._split_chain_for_nodes(
                                chain, ct.nickname
                            )
                            nodes.extend(ct_nodes)
                            video_queue.extend(ct_videos)

                    # 发送合并转发消息
                    if nodes:
                        batch_label = ""
                        if len(author_batches) > 1:
                            batch_label = (
                                f"（第{batch_idx + 1}/{len(author_batches)}批）"
                            )
                        try:
                            message_chain = MessageChain(chain=[Nodes(nodes)])
                            await self.context.send_message(umo, message_chain)
                            logger.info(
                                f"集体转发已推送至 {umo} "
                                f"{batch_label}共 {len(nodes)} 个节点"
                            )
                        except Exception as node_err:
                            logger.warning(
                                f"集体合并转发失败，回退逐条发送: {node_err}"
                            )
                            # 回退：逐条发送
                            for ct in [
                                ct
                                for a in batch_authors
                                for ct in seen_authors[a]
                            ]:
                                await self._send_tweet_to_subscriber(
                                    umo,
                                    ct.username,
                                    ct.tweet_info,
                                    ct.sub_config,
                                    ct.nickname,
                                    translated_text=ct.translated_text,
                                    translate_model=ct.translate_model,
                                )
                            # 回退模式下跳过独立视频发送（已在逐条发送中处理）
                            video_queue.clear()

                    # 逐条发送视频（独立消息，避免超时）
                    for vid_comp in video_queue:
                        await self._send_video_or_fallback(umo, vid_comp)

            except Exception as e:
                logger.error(f"集体转发推送至 {umo} 失败: {e}")
                # 回退：逐条发送该订阅者的缓存推文
                for ct in valid_tweets:
                    try:
                        await self._send_tweet_to_subscriber(
                            umo,
                            ct.username,
                            ct.tweet_info,
                            ct.sub_config,
                            ct.nickname,
                            translated_text=ct.translated_text,
                            translate_model=ct.translate_model,
                        )
                    except Exception as fallback_err:
                        logger.error(
                            f"集体转发回退逐条发送也失败: {fallback_err}"
                        )

    async def _poll_tweets(self):
        """定时轮询推文"""
        while self._running:
            try:
                await self._check_all_subscriptions()
            except Exception as e:
                logger.error(f"推文轮询出错: {e}")
            await asyncio.sleep(self.poll_interval * 60)

    async def _check_all_subscriptions(self):
        """检查所有订阅的新推文"""
        subscribe_list = await self._get_subs()
        if not subscribe_list:
            return

        results: list[bool] = []
        for username, info in subscribe_list.items():
            try:
                result = await self._check_user_tweets(username, info)
                results.append(result)
                await asyncio.sleep(3)  # 避免频繁请求
            except Exception as e:
                logger.error(f"检查 {username} 推文失败: {e}")
                results.append(False)

        # 集体转发模式：轮询结束后统一发送缓存的推文
        if self.collective_forward and self._collected_tweets:
            await self._flush_collected_tweets()

        # 自动切换镜像站
        if (
            self.data_provider == DATA_PROVIDER_NITTER
            and not self.custom_nitter_url
            and results
        ):
            success_count = sum(1 for r in results if r)
            if success_count < len(results) / 2 and self.website_list:
                new_url = get_next_website(
                    self.website_list, self.twitter_api.nitter_url
                )
                if new_url and new_url != self.twitter_api.nitter_url:
                    logger.info(f"当前镜像站出错过多，切换至: {new_url}")
                    self.twitter_api.nitter_url = new_url

    async def _check_user_tweets(self, username: str, info: dict) -> bool:
        """检查某个用户的新推文，返回是否成功获取"""
        try:
            since_id = info.get("since_id", "")
            new_tweet_items = await self.twitter_api.get_user_timeline_items(
                username, since_id
            )

            if not new_tweet_items:
                return True

            # 再次确认该推主仍有订阅者（可能在获取推文期间被取关）
            latest_subs = await self._get_subs()
            if username not in latest_subs:
                logger.info(f"@{username} 已无订阅者，跳过推送")
                return True

            # 按时间正序（最旧在前）逐条处理
            # （集体转发模式下缓存，即时模式下直接推送）
            last_processed_id = str(since_id or "")
            detail_failed = False
            for item in new_tweet_items:
                tweet_id = str(item.get("tweet_id") or "")
                tweet_username = str(item.get("username") or username)
                if not tweet_id:
                    continue

                if item.get("is_retweet") and not self.include_retweets:
                    logger.debug(
                        f"跳过 @{username} 转帖: {tweet_id}"
                    )
                    last_processed_id = tweet_id
                    continue

                tweet_info = await self.twitter_api.get_tweet(
                    tweet_username, tweet_id
                )
                if not tweet_info.get("status", True):
                    logger.warning(
                        f"获取 @{username} 推文详情失败，保留游标等待下次重试: "
                        f"{tweet_id}"
                    )
                    detail_failed = True
                    break
                self._attach_timeline_item_metadata(tweet_info, item)
                await self._push_tweet_to_subscribers(username, tweet_info, info)
                last_processed_id = tweet_id

            # 仅推进到已经成功处理（或按配置明确跳过）的最后一条，
            # 避免详情 API 临时失败时越过未发送的推文。
            subs = await self._get_subs()
            if username in subs and last_processed_id != str(since_id or ""):
                subs[username]["since_id"] = last_processed_id
                await self._save_subs(subs)

            return not detail_failed
        except FxTwitterTimelineError as e:
            logger.warning(f"获取 @{username} 时间线失败，保留当前游标: {e}")
            return False
        except Exception as e:
            logger.error(f"获取 {username} 推文异常: {e}")
            return False

    # ========== 指令处理 ==========

    @filter.command("推特关注", alias={"twitter_follow"})
    async def follow_twitter(self, event: AstrMessageEvent, username: str = ""):
        """订阅推主，格式: /推特关注 <推主id> [r18] [媒体]"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        if not username:
            yield event.plain_result("请提供推主ID，用法: /推特关注 <推主ID> [r18] [媒体]")
            return

        username = username.strip("@").strip()

        # 解析可选参数
        msg_str = event.message_str
        extra_args = msg_str.strip().split()[2:]  # 跳过指令名和用户名
        r18 = "r18" in extra_args
        media_only = "媒体" in extra_args

        # 获取用户信息
        user_info = await self.twitter_api.get_user_info(username)
        if not user_info["status"]:
            yield event.plain_result(f"未找到用户: {username}")
            return

        # 获取最新推文 ID 作为 since_id
        try:
            latest_ids = await self.twitter_api.get_user_newtimeline(username)
        except FxTwitterTimelineError as e:
            logger.warning(f"订阅 @{username} 时获取时间线失败: {e}")
            yield event.plain_result(f"获取 @{username} 时间线失败，请稍后重试")
            return
        since_id = latest_ids[-1] if latest_ids else ""

        umo = event.unified_msg_origin

        # 添加订阅
        subs = await self._get_subs()
        session_config = {
            "status": True,
            "r18": r18,
            "media": media_only,
        }

        if username not in subs:
            subs[username] = {
                "screen_name": user_info["screen_name"],
                "since_id": since_id,
                "subscribers": {},
            }

        subs[username]["subscribers"][umo] = session_config
        subs[username]["screen_name"] = user_info["screen_name"]
        if since_id:
            subs[username]["since_id"] = since_id

        await self._save_subs(subs)

        r18_str = " | R18" if r18 else ""
        media_str = " | 仅媒体" if media_only else ""
        bio = user_info["bio"][:100] + ("..." if len(user_info["bio"]) > 100 else "")
        result = (
            f"订阅成功!\n"
            f"ID: {username}\n"
            f"昵称: {user_info['screen_name']}\n"
            f"简介: {bio}\n"
            f"选项: {r18_str}{media_str}"
        )
        yield event.plain_result(result)

    @filter.command("推特批量关注", alias={"twitter_batch_follow"})
    async def batch_follow_twitter(self, event: AstrMessageEvent):
        """批量订阅推主，格式: /推特批量关注 <推主id1> <推主id2> ... [r18] [媒体]"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        # 解析消息：提取用户名和选项
        msg_str = event.message_str.strip()
        tokens = msg_str.split()[1:]  # 跳过指令名
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量关注 <推主ID1> <推主ID2> ... [r18] [媒体]"
            )
            return

        r18 = "r18" in tokens
        media_only = "媒体" in tokens
        usernames = [t.strip("@").strip() for t in tokens if t not in ("r18", "媒体")]

        if not usernames:
            yield event.plain_result("请提供至少一个推主ID")
            return

        yield event.plain_result(f"正在批量订阅 {len(usernames)} 个推主，请稍候...")

        umo = event.unified_msg_origin
        subs = await self._get_subs()
        results: list[str] = []
        success_count = 0

        for username in usernames:
            try:
                # 获取用户信息
                user_info = await self.twitter_api.get_user_info(username)
                if not user_info["status"]:
                    results.append(f"❌ @{username} - 未找到用户")
                    continue

                # 获取最新推文 ID
                latest_ids = await self.twitter_api.get_user_newtimeline(username)
                since_id = latest_ids[-1] if latest_ids else ""

                # 添加订阅
                session_config = {
                    "status": True,
                    "r18": r18,
                    "media": media_only,
                }

                if username not in subs:
                    subs[username] = {
                        "screen_name": user_info["screen_name"],
                        "since_id": since_id,
                        "subscribers": {},
                    }

                subs[username]["subscribers"][umo] = session_config
                subs[username]["screen_name"] = user_info["screen_name"]
                if since_id:
                    subs[username]["since_id"] = since_id

                success_count += 1
                r18_str = " | R18" if r18 else ""
                media_str = " | 仅媒体" if media_only else ""
                results.append(
                    f"✅ @{username} ({user_info['screen_name']}){r18_str}{media_str}"
                )

            except Exception as e:
                results.append(f"❌ @{username} - 订阅失败: {e}")

        # 一次性保存所有变更
        await self._save_subs(subs)

        # 汇总结果
        summary = (
            f"批量订阅完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )
        yield event.plain_result(summary)

    @filter.command("推特取关", alias={"twitter_unfollow"})
    async def unfollow_twitter(self, event: AstrMessageEvent, username: str = ""):
        """取关推主，格式: /推特取关 <推主id>"""
        if not username:
            yield event.plain_result("请提供推主ID，用法: /推特取关 <推主ID>")
            return

        username = username.strip("@").strip()
        umo = event.unified_msg_origin

        subs = await self._get_subs()
        if username not in subs:
            yield event.plain_result(f"未订阅推主: {username}")
            return

        if umo not in subs[username].get("subscribers", {}):
            yield event.plain_result(f"当前会话未订阅 {username}")
            return

        subs[username]["subscribers"].pop(umo)

        # 如果该推主没有任何订阅者了，删除该推主
        if not subs[username].get("subscribers", {}):
            subs.pop(username)

        await self._save_subs(subs)
        yield event.plain_result(f"已取关 {username}")

    @filter.command("推特批量取关", alias={"twitter_batch_unfollow"})
    async def batch_unfollow_twitter(self, event: AstrMessageEvent):
        """批量取关推主，格式: /推特批量取关 <推主id1> <推主id2> ..."""
        msg_str = event.message_str.strip()
        tokens = msg_str.split()[1:]  # 跳过指令名
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量取关 <推主ID1> <推主ID2> ..."
            )
            return

        usernames = [t.strip("@").strip() for t in tokens]
        umo = event.unified_msg_origin
        subs = await self._get_subs()

        results: list[str] = []
        success_count = 0

        for username in usernames:
            if username not in subs:
                results.append(f"❌ @{username} - 未订阅此推主")
                continue

            if umo not in subs[username].get("subscribers", {}):
                results.append(f"❌ @{username} - 当前会话未订阅")
                continue

            subs[username]["subscribers"].pop(umo)

            # 如果该推主没有任何订阅者了，删除该推主
            if not subs[username].get("subscribers", {}):
                subs.pop(username)

            success_count += 1
            results.append(f"✅ @{username} - 已取关")

        # 一次性保存所有变更
        await self._save_subs(subs)

        # 汇总结果
        summary = (
            f"批量取关完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )
        yield event.plain_result(summary)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("推特清空订阅", alias={"twitter_clear_all"})
    async def clear_all_subscriptions(self, event: AstrMessageEvent):
        """清空所有推文订阅（仅管理员），格式: /推特清空订阅"""
        subs = await self._get_subs()
        if not subs:
            yield event.plain_result("当前没有任何订阅")
            return

        total_authors = len(subs)
        total_subscribers = sum(
            len(info.get("subscribers", {})) for info in subs.values()
        )

        await self._save_subs({})
        # 清空集体转发缓存
        self._collected_tweets.clear()

        yield event.plain_result(
            f"已清空所有订阅: 共 {total_authors} 个推主, "
            f"{total_subscribers} 个订阅关系"
        )

    @filter.command("推特列表", alias={"twitter_list"})
    async def list_follows(self, event: AstrMessageEvent):
        """查看当前订阅的推主列表"""
        umo = event.unified_msg_origin
        subs = await self._get_subs()

        lines = []
        for username, info in subs.items():
            subscribers = info.get("subscribers", {})
            if umo in subscribers:
                sub = subscribers[umo]
                status_icon = "🟢" if sub.get("status", True) else "🔴"
                r18_str = " | R18" if sub.get("r18") else ""
                media_str = " | 仅媒体" if sub.get("media") else ""
                screen_name = info.get("screen_name", username)
                lines.append(
                    f"{status_icon} @{username} ({screen_name}){r18_str}{media_str}"
                )

        if not lines:
            yield event.plain_result("当前没有订阅任何推主")
            return

        yield event.plain_result("当前订阅列表:\n" + "\n".join(
            f"{i}. {line}" for i, line in enumerate(lines, 1)
        ))

    @filter.command("推特推送", alias={"twitter_push"})
    async def toggle_push(self, event: AstrMessageEvent, action: str = ""):
        """开启/关闭推文推送，格式: /推特推送 开启 或 /推特推送 关闭"""
        if action not in ("开启", "关闭"):
            yield event.plain_result("用法: /推特推送 开启 或 /推特推送 关闭")
            return

        enabled = action == "开启"
        umo = event.unified_msg_origin

        subs = await self._get_subs()
        count = 0
        for username in subs:
            subscribers = subs[username].get("subscribers", {})
            if umo in subscribers:
                subs[username]["subscribers"][umo]["status"] = enabled
                count += 1

        if count > 0:
            await self._save_subs(subs)
            status_text = "开启" if enabled else "关闭"
            yield event.plain_result(f"推文推送已{status_text} (影响 {count} 个订阅)")
        else:
            yield event.plain_result("当前没有订阅任何推主")

    @filter.command("推特测试", alias={"twitter_test"})
    async def test_tweet(self, event: AstrMessageEvent, username: str = ""):
        """立即获取并推送指定推主的最新一条推文，格式: /推特测试 <推主id>"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        if not username:
            yield event.plain_result("请提供推主ID，用法: /推特测试 <推主ID>")
            return

        username = username.strip("@").strip()

        umo = event.unified_msg_origin

        yield event.plain_result(f"正在获取 @{username} 的最新推文，请稍候...")

        # 获取时间线并按配置选择最新推文
        try:
            timeline_items = await self.twitter_api.get_user_timeline_items(username)
        except FxTwitterTimelineError as e:
            logger.warning(f"测试 @{username} 时获取时间线失败: {e}")
            yield event.plain_result(f"获取 @{username} 时间线失败，请稍后重试")
            return
        if not timeline_items:
            yield event.plain_result(f"未找到 @{username} 的推文")
            return

        selected_item = None
        for item in timeline_items:
            if item.get("is_retweet") and not self.include_retweets:
                continue
            selected_item = item
            break

        if not selected_item:
            yield event.plain_result(f"未找到 @{username} 的非转贴推文")
            return

        tweet_id = str(selected_item.get("tweet_id") or "")
        tweet_username = str(selected_item.get("username") or username)

        # 获取推文详情
        tweet_info = await self.twitter_api.get_tweet(tweet_username, tweet_id)
        if not tweet_info.get("status", True):
            yield event.plain_result(
                f"无法获取 @{username} 的推文，帖子可能已删除、受限或暂时不可用"
            )
            return
        self._attach_timeline_item_metadata(tweet_info, selected_item)

        # 翻译推文
        translated_text, translate_model = await self._maybe_translate(
            tweet_info, umo
        )

        # 构建并返回消息
        chain = await self._build_tweet_message_chain(
            username, tweet_info,
            translated_text=translated_text,
            translate_model=translate_model,
        )
        if not chain:
            yield event.plain_result(f"未找到 @{username} 的推文内容")
            return

        if self.use_node:
            # 合并转发模式
            author_username = str(tweet_info.get("username") or username)
            screen_name = str(tweet_info.get("screen_name") or author_username)
            nickname = self._build_author_display(author_username, screen_name)
            try:
                nodes, video_parts = self._split_chain_for_nodes(chain, nickname)
                if nodes:
                    yield event.chain_result([Nodes(nodes)])
                else:
                    yield event.plain_result(f"未找到 @{username} 的推文内容")
                # 视频无法通过 yield 发送，作为独立消息发送
                for vid_comp in video_parts:
                    await self._send_video_or_fallback(umo, vid_comp)
            except Exception:
                # 合并转发失败，回退到普通消息链
                plain_chain, video_parts = self._split_plain_chain_and_videos(chain)
                if plain_chain:
                    yield event.chain_result(plain_chain)
                for vid_comp in video_parts:
                    await self._send_video_or_fallback(umo, vid_comp)
        else:
            plain_chain, video_parts = self._split_plain_chain_and_videos(chain)
            if plain_chain:
                yield event.chain_result(plain_chain)
            for vid_comp in video_parts:
                await self._send_video_or_fallback(umo, vid_comp)

    # ========== 链接识别 ==========

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，检测 Twitter/X 链接并解析推文"""
        # 全局链接识别开关检查
        if not self.link_recognition_enabled:
            return

        umo = event.unified_msg_origin
        msg_str = event.message_str

        # 检测是否包含 Twitter/X 链接
        match = TWITTER_LINK_PATTERN.search(msg_str)
        if not match:
            return

        link = match.group(1)
        username = match.group(2)
        tweet_id = match.group(3)

        logger.info(f"检测到推文链接: {link}")

        if not self._provider_ready:
            return

        try:
            tweet_info = await self.twitter_api.get_tweet(username, tweet_id)
            if not tweet_info.get("status", True):
                yield event.plain_result(
                    "无法获取该推文，帖子可能已删除、受限或暂时不可用"
                )
                return

            # 翻译推文
            translated_text, translate_model = await self._maybe_translate(
                tweet_info, umo
            )

            # 构建推文消息链
            chain = await self._build_tweet_message_chain(
                username,
                tweet_info,
                {"r18": True, "media": False, "status": True},
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                return

            if self.use_node:
                # 合并转发模式
                author_username = str(tweet_info.get("username") or username)
                screen_name = str(tweet_info.get("screen_name") or author_username)
                nickname = self._build_author_display(author_username, screen_name)
                try:
                    nodes, video_parts = self._split_chain_for_nodes(
                        chain, nickname
                    )
                    if nodes:
                        yield event.chain_result([Nodes(nodes)])
                    # 视频无法通过 yield 发送，作为独立消息发送
                    for vid_comp in video_parts:
                        await self._send_video_or_fallback(umo, vid_comp)
                except Exception:
                    plain_chain, video_parts = self._split_plain_chain_and_videos(chain)
                    if plain_chain:
                        yield event.chain_result(plain_chain)
                    for vid_comp in video_parts:
                        await self._send_video_or_fallback(umo, vid_comp)
            else:
                plain_chain, video_parts = self._split_plain_chain_and_videos(chain)
                if plain_chain:
                    yield event.chain_result(plain_chain)
                for vid_comp in video_parts:
                    await self._send_video_or_fallback(umo, vid_comp)

        except Exception as e:
            logger.error(f"解析推文链接失败: {e}")
