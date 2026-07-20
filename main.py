"""
AstrBot Twitter 推文转发插件

支持 Nitter 与 FxTwitter API 数据源，以及订阅、定时推送、链接识别、
合并转发消息和推文翻译。
"""

import asyncio
import copy
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .services import (
    PollingService,
    PollingSettings,
    SubscriptionService,
    TweetDeliveryService,
    TweetDeliverySettings,
    TweetMessageService,
    TweetMessageSettings,
)
from .twitter_api import (
    DATA_PROVIDER_FXTWITTER,
    DATA_PROVIDER_NITTER,
    DATA_PROVIDER_OPTIONS,
    DEFAULT_FXTWITTER_API_BASE,
    FxTwitterTimelineError,
    TwitterAPI,
    WEBSITE_LIST,
)

try:
    from .twitter_webui import TwitterWebUIController
except ModuleNotFoundError as exc:
    if exc.name != "astrbot.api.web":
        raise
    TwitterWebUIController = None


TWITTER_LINK_PATTERN = re.compile(
    r"(https?://(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+))"
)


class TwitterPlugin(Star):
    """Twitter 推文转发插件主类。"""

    def _cfg(self, block: str, key: str, default, *legacy_keys: str):
        """读取分组配置，并兼容旧版顶层扁平配置。"""
        block_config = self.config.get(block, {}) or {}
        if isinstance(block_config, dict):
            value = block_config.get(key)
            if value is not None:
                return value

        for config_key in (key, *legacy_keys):
            value = self.config.get(config_key)
            if value is not None:
                return value

        return default

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.proxy = str(self._cfg("basic", "twitter_proxy", "") or "") or None
        self.data_provider = str(
            self._cfg(
                "basic",
                "twitter_data_provider",
                DATA_PROVIDER_NITTER,
            )
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
        self.use_node = bool(
            self._cfg("message_format", "twitter_use_node", True)
        )
        self.no_text = bool(
            self._cfg("message_format", "twitter_no_text", False)
        )
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
            1,
            int(self._cfg("basic", "twitter_poll_interval", 5)),
        )
        self.poll_max_tweets_per_user = max(
            1,
            int(
                self._cfg(
                    "basic",
                    "twitter_poll_max_tweets_per_user",
                    5,
                )
            ),
        )
        self.collective_forward = bool(
            self._cfg(
                "message_format",
                "twitter_collective_forward",
                False,
            )
        )
        self.include_retweets = bool(
            self._cfg(
                "content_filter",
                "twitter_include_retweets",
                True,
            )
        )
        self.deduplicate_retweets = bool(
            self._cfg(
                "content_filter",
                "twitter_deduplicate_retweets",
                False,
            )
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
            self._cfg(
                "translation",
                "twitter_translate_enabled",
                False,
            )
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
            self._cfg(
                "translation",
                "twitter_translate_provider_id",
                "",
            )
            or ""
        ).strip()
        self.translate_custom_prompt_enabled = bool(
            self._cfg(
                "translation",
                "twitter_translate_custom_prompt_enabled",
                False,
            )
        )
        self.translate_custom_prompt = str(
            self._cfg(
                "translation",
                "twitter_translate_custom_prompt",
                "",
            )
            or ""
        ).strip()
        self.custom_nitter_url = str(
            self._cfg("basic", "twitter_nitter_url", "") or ""
        ).strip()
        self.image_quality = str(
            self._cfg(
                "message_format",
                "twitter_image_quality",
                "orig",
            )
            or "orig"
        ).strip()
        self.pre_download_media = bool(
            self._cfg("basic", "twitter_pre_download_media", False)
        )

        self.website_list: list[str] = []
        if self.data_provider == DATA_PROVIDER_NITTER:
            if self.custom_nitter_url:
                self.website_list.append(self.custom_nitter_url)
            self.website_list.extend(WEBSITE_LIST)

        self.twitter_api = TwitterAPI(
            proxy=self.proxy,
            nitter_url="",
            image_quality=self.image_quality,
            provider=self.data_provider,
            fxtwitter_api_base=self.fxtwitter_api_base,
        )
        self._provider_ready = False

        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._poll_wakeup = asyncio.Event()
        self._config_lock = asyncio.Lock()

        self.subscription_service = SubscriptionService(
            self._get_kv_data,
            self._put_kv_data,
            self.twitter_api,
            lambda: self._provider_ready,
        )
        self.message_service = TweetMessageService(
            context,
            self.twitter_api,
            self._render_tweet_html,
            TweetMessageSettings(
                no_text=self.no_text,
                send_media_separately=self.send_media_separately,
                include_tweet_link=self.include_tweet_link,
                text_render_mode=self.text_render_mode,
                screenshot_theme=self.screenshot_theme,
                video_max_size_mb=self.video_max_size_mb,
                translate_enabled=self.translate_enabled,
                translate_target_lang=self.translate_target_lang,
                translate_provider_id=self.translate_provider_id,
                translate_custom_prompt_enabled=(
                    self.translate_custom_prompt_enabled
                ),
                translate_custom_prompt=self.translate_custom_prompt,
                pre_download_media=self.pre_download_media,
                proxy=self.proxy,
            ),
        )
        self.delivery_service = TweetDeliveryService(
            context,
            self.subscription_service,
            self.message_service,
            TweetDeliverySettings(
                use_node=self.use_node,
                collective_forward=self.collective_forward,
                collective_max_authors=self.collective_max_authors,
                deduplicate_retweets=self.deduplicate_retweets,
            ),
        )
        self.polling_service = PollingService(
            self.twitter_api,
            self.subscription_service,
            self.delivery_service,
            PollingSettings(
                include_retweets=self.include_retweets,
                data_provider=self.data_provider,
                custom_nitter_url=self.custom_nitter_url,
                website_list=tuple(self.website_list),
                max_tweets_per_user=self.poll_max_tweets_per_user,
            ),
        )

        self._webui_controller = None
        register_web_api = getattr(context, "register_web_api", None)
        if TwitterWebUIController is not None and callable(register_web_api):
            try:
                self._webui_controller = TwitterWebUIController(self, context)
            except Exception as exc:
                logger.warning(f"Twitter 订阅管理 WebUI 注册失败: {exc}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过订阅管理 WebUI")

    async def _get_kv_data(self, key: str, default: Any) -> Any:
        """延迟调用 AstrBot KV 接口，便于服务独立测试。"""
        return await self.get_kv_data(key, default)

    async def _put_kv_data(self, key: str, value: Any) -> None:
        """延迟调用 AstrBot KV 写入接口。"""
        await self.put_kv_data(key, value)

    async def _render_tweet_html(self, *args, **kwargs):
        """延迟调用 AstrBot HTML 渲染接口。"""
        return await self.html_render(*args, **kwargs)

    async def initialize(self):
        """初始化数据源并启动轮询任务。"""
        logger.info("Twitter 推文转发插件初始化中...")

        if self.collective_forward and not self.use_node:
            logger.warning(
                "集体转发模式已开启但合并转发消息未开启，集体转发功能不会生效。"
                "请同时开启「使用合并转发消息」配置项。"
            )

        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            logger.info("当前使用 Twitter 数据源: FxTwitter API")
            self._provider_ready = (
                await self.twitter_api.check_fxtwitter_available()
            )
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

        if self._provider_ready:
            self._running = True
            self._poll_task = asyncio.create_task(self._poll_tweets())
            logger.info(f"推文轮询已启动，间隔 {self.poll_interval} 分钟")

        logger.info("Twitter 推文转发插件初始化完成")

    async def terminate(self):
        """停止后台任务，发送剩余缓存并关闭 HTTP 客户端。"""
        self._running = False
        self._poll_wakeup.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if (
            self.delivery_service.has_collected
            or self.polling_service.has_pending_collective
        ):
            logger.info("正在发送剩余缓存的推文...")
            await self.polling_service.flush_pending_collective()
        await self.twitter_api.close()
        logger.info("Twitter 推文转发插件已停止")

    # WebUI 与现有调用方使用的兼容委托。
    async def _get_subs(self) -> dict:
        return await self.subscription_service.get_all()

    async def _save_subs(self, data: dict) -> None:
        await self.subscription_service.save_all(data)

    async def _get_subscriptions_snapshot(self) -> dict:
        return await self.subscription_service.get_snapshot()

    @staticmethod
    def _find_subscription_key(subs: dict, username: str) -> str | None:
        return SubscriptionService.find_key(subs, username)

    async def _add_subscription(
        self,
        umo: str,
        username: str,
        *,
        r18: bool = False,
        media_only: bool = False,
        reject_duplicate: bool = False,
    ) -> dict:
        return await self.subscription_service.add(
            umo,
            username,
            r18=r18,
            media_only=media_only,
            reject_duplicate=reject_duplicate,
        )

    async def _update_subscription(
        self,
        umo: str,
        username: str,
        changes: dict,
    ) -> dict:
        return await self.subscription_service.update(umo, username, changes)

    async def _remove_subscription(self, umo: str, username: str) -> dict:
        return await self.subscription_service.remove(umo, username)

    async def _set_session_subscriptions_status(
        self,
        umo: str,
        enabled: bool,
    ) -> int:
        return await self.subscription_service.set_session_status(umo, enabled)

    async def _clear_subscriptions(self) -> dict:
        return await self.subscription_service.clear()

    async def _update_subscription_cursor(
        self,
        username: str,
        since_id: str,
    ) -> bool:
        return await self.subscription_service.update_cursor(username, since_id)

    @staticmethod
    def _attach_timeline_item_metadata(tweet_info: dict, item: dict) -> None:
        PollingService.attach_timeline_item_metadata(tweet_info, item)

    async def _check_all_subscriptions(self) -> None:
        await self.polling_service.check_all()

    async def _check_user_tweets(self, username: str, info: dict) -> bool:
        return await self.polling_service.check_user(username, info)

    def _provider_unavailable_message(self) -> str:
        if self.data_provider == DATA_PROVIDER_FXTWITTER:
            return "FxTwitter API 不可用，请检查配置或网络"
        return "Nitter 镜像站不可用，请检查配置或网络"

    async def _set_poll_interval(self, minutes: int) -> None:
        """持久化全局轮询间隔，并重置下一次轮询倒计时。"""
        config_lock = getattr(self, "_config_lock", None)
        if config_lock is None:
            config_lock = asyncio.Lock()
            self._config_lock = config_lock

        async with config_lock:
            had_basic = "basic" in self.config
            previous_basic = copy.deepcopy(self.config.get("basic"))
            basic = self.config.get("basic")
            if not isinstance(basic, dict):
                basic = {}
                self.config["basic"] = basic
            basic["twitter_poll_interval"] = minutes

            try:
                save_config = getattr(self.config, "save_config", None)
                if not callable(save_config):
                    raise RuntimeError("当前配置对象不支持持久化")
                save_result = save_config()
                if asyncio.iscoroutine(save_result):
                    await save_result
            except Exception:
                if had_basic:
                    self.config["basic"] = previous_basic
                else:
                    self.config.pop("basic", None)
                raise

            self.poll_interval = minutes
            self._poll_wakeup.set()

    async def _wait_for_next_poll(self) -> None:
        """等待下一轮轮询；配置更新会重置完整倒计时。"""
        while self._running:
            self._poll_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._poll_wakeup.wait(),
                    timeout=self.poll_interval * 60,
                )
            except TimeoutError:
                return

    async def _poll_tweets(self) -> None:
        """按全局间隔执行轮询。"""
        while self._running:
            try:
                await self._check_all_subscriptions()
            except Exception as exc:
                logger.error(f"推文轮询出错: {exc}")
            await self._wait_for_next_poll()

    @filter.command("推特关注", alias={"twitter_follow"})
    async def follow_twitter(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """订阅推主，格式: /推特关注 <推主id> [r18] [媒体]。"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        if not username:
            yield event.plain_result(
                "请提供推主ID，用法: /推特关注 <推主ID> [r18] [媒体]"
            )
            return

        username = username.strip("@").strip()
        extra_args = event.message_str.strip().split()[2:]
        r18 = "r18" in extra_args
        media_only = "媒体" in extra_args

        try:
            add_result = await self._add_subscription(
                event.unified_msg_origin,
                username,
                r18=r18,
                media_only=media_only,
            )
        except FxTwitterTimelineError as exc:
            logger.warning(f"订阅 @{username} 时获取时间线失败: {exc}")
            yield event.plain_result(f"获取 @{username} 时间线失败，请稍后重试")
            return
        except Exception as exc:
            logger.warning(f"订阅 @{username} 失败: {exc}")
            yield event.plain_result(f"订阅 @{username} 失败，请稍后重试")
            return

        reason = add_result.get("reason")
        if reason == "not_found":
            yield event.plain_result(f"未找到用户: {username}")
            return
        if reason == "provider_unavailable":
            yield event.plain_result(self._provider_unavailable_message())
            return

        username = add_result["username"]
        screen_name = add_result["screen_name"]
        r18_str = " | R18" if r18 else ""
        media_str = " | 仅媒体" if media_only else ""
        raw_bio = add_result.get("bio", "")
        bio = raw_bio[:100] + ("..." if len(raw_bio) > 100 else "")
        yield event.plain_result(
            f"订阅成功!\n"
            f"ID: {username}\n"
            f"昵称: {screen_name}\n"
            f"简介: {bio}\n"
            f"选项: {r18_str}{media_str}"
        )

    @filter.command("推特批量关注", alias={"twitter_batch_follow"})
    async def batch_follow_twitter(self, event: AstrMessageEvent):
        """批量订阅推主。"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return

        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量关注 "
                "<推主ID1> <推主ID2> ... [r18] [媒体]"
            )
            return

        r18 = "r18" in tokens
        media_only = "媒体" in tokens
        usernames = [
            token.strip("@").strip()
            for token in tokens
            if token not in ("r18", "媒体")
        ]
        if not usernames:
            yield event.plain_result("请提供至少一个推主ID")
            return

        yield event.plain_result(
            f"正在批量订阅 {len(usernames)} 个推主，请稍候..."
        )

        umo = event.unified_msg_origin
        results: list[str] = []
        success_count = 0
        for username in usernames:
            try:
                add_result = await self._add_subscription(
                    umo,
                    username,
                    r18=r18,
                    media_only=media_only,
                )
                if add_result.get("reason") == "not_found":
                    results.append(f"❌ @{username} - 未找到用户")
                    continue
                if add_result.get("reason") == "provider_unavailable":
                    results.append(f"❌ @{username} - 数据源不可用")
                    continue

                success_count += 1
                r18_str = " | R18" if r18 else ""
                media_str = " | 仅媒体" if media_only else ""
                results.append(
                    f"✅ @{add_result['username']} "
                    f"({add_result['screen_name']}){r18_str}{media_str}"
                )
            except FxTwitterTimelineError as exc:
                logger.warning(
                    f"批量订阅 @{username} 时获取时间线失败: {exc}"
                )
                results.append(f"❌ @{username} - 获取时间线失败")
            except Exception as exc:
                results.append(f"❌ @{username} - 订阅失败: {exc}")

        yield event.plain_result(
            f"批量订阅完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )

    @filter.command("推特取关", alias={"twitter_unfollow"})
    async def unfollow_twitter(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """取关推主。"""
        if not username:
            yield event.plain_result("请提供推主ID，用法: /推特取关 <推主ID>")
            return

        username = username.strip("@").strip()
        remove_result = await self._remove_subscription(
            event.unified_msg_origin,
            username,
        )
        if not remove_result["ok"]:
            yield event.plain_result(f"当前会话未订阅 {username}")
            return
        yield event.plain_result(f"已取关 {remove_result['username']}")

    @filter.command("推特批量取关", alias={"twitter_batch_unfollow"})
    async def batch_unfollow_twitter(self, event: AstrMessageEvent):
        """批量取关推主。"""
        tokens = event.message_str.strip().split()[1:]
        if not tokens:
            yield event.plain_result(
                "请提供推主ID，用法: /推特批量取关 "
                "<推主ID1> <推主ID2> ..."
            )
            return

        usernames = [token.strip("@").strip() for token in tokens]
        umo = event.unified_msg_origin
        results: list[str] = []
        success_count = 0
        for username in usernames:
            remove_result = await self._remove_subscription(umo, username)
            if not remove_result["ok"]:
                results.append(f"❌ @{username} - 当前会话未订阅")
                continue
            success_count += 1
            results.append(f"✅ @{remove_result['username']} - 已取关")

        yield event.plain_result(
            f"批量取关完成: 成功 {success_count}/{len(usernames)}\n"
            + "\n".join(results)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("推特清空订阅", alias={"twitter_clear_all"})
    async def clear_all_subscriptions(self, event: AstrMessageEvent):
        """清空所有推文订阅。"""
        cleared = await self._clear_subscriptions()
        if not cleared["authors"]:
            yield event.plain_result("当前没有任何订阅")
            return

        self.delivery_service.clear_collected()
        yield event.plain_result(
            f"已清空所有订阅: 共 {cleared['authors']} 个推主, "
            f"{cleared['relations']} 个订阅关系"
        )

    @filter.command("推特列表", alias={"twitter_list"})
    async def list_follows(self, event: AstrMessageEvent):
        """查看当前会话订阅的推主列表。"""
        umo = event.unified_msg_origin
        subs = await self._get_subs()
        lines = []
        for username, info in subs.items():
            subscribers = info.get("subscribers", {})
            if umo not in subscribers:
                continue
            sub_config = subscribers[umo]
            status_icon = "🟢" if sub_config.get("status", True) else "🔴"
            r18_str = " | R18" if sub_config.get("r18") else ""
            media_str = " | 仅媒体" if sub_config.get("media") else ""
            screen_name = info.get("screen_name", username)
            lines.append(
                f"{status_icon} @{username} ({screen_name})"
                f"{r18_str}{media_str}"
            )

        if not lines:
            yield event.plain_result("当前没有订阅任何推主")
            return

        yield event.plain_result(
            "当前订阅列表:\n"
            + "\n".join(
                f"{index}. {line}"
                for index, line in enumerate(lines, 1)
            )
        )

    @filter.command("推特推送", alias={"twitter_push"})
    async def toggle_push(
        self,
        event: AstrMessageEvent,
        action: str = "",
    ):
        """开启或关闭当前会话的全部推文推送。"""
        if action not in ("开启", "关闭"):
            yield event.plain_result("用法: /推特推送 开启 或 /推特推送 关闭")
            return

        enabled = action == "开启"
        count = await self._set_session_subscriptions_status(
            event.unified_msg_origin,
            enabled,
        )
        if count > 0:
            status_text = "开启" if enabled else "关闭"
            yield event.plain_result(
                f"推文推送已{status_text} (影响 {count} 个订阅)"
            )
        else:
            yield event.plain_result("当前没有订阅任何推主")

    @filter.command("推特测试", alias={"twitter_test"})
    async def test_tweet(
        self,
        event: AstrMessageEvent,
        username: str = "",
    ):
        """立即获取并推送指定推主的最新一条推文。"""
        if not self._provider_ready:
            yield event.plain_result(self._provider_unavailable_message())
            return
        if not username:
            yield event.plain_result(
                "请提供推主ID，用法: /推特测试 <推主ID>"
            )
            return

        username = username.strip("@").strip()
        umo = event.unified_msg_origin
        yield event.plain_result(
            f"正在获取 @{username} 的最新推文，请稍候..."
        )

        try:
            timeline_items = await self.twitter_api.get_user_timeline_items(
                username
            )
        except FxTwitterTimelineError as exc:
            logger.warning(f"测试 @{username} 时获取时间线失败: {exc}")
            yield event.plain_result(
                f"获取 @{username} 时间线失败，请稍后重试"
            )
            return
        if not timeline_items:
            yield event.plain_result(f"未找到 @{username} 的推文")
            return

        selected_item = next(
            (
                item
                for item in timeline_items
                if self.include_retweets or not item.get("is_retweet")
            ),
            None,
        )
        if not selected_item:
            yield event.plain_result(f"未找到 @{username} 的非转贴推文")
            return

        tweet_id = str(selected_item.get("tweet_id") or "")
        tweet_username = str(selected_item.get("username") or username)
        tweet_info = await self.twitter_api.get_tweet(
            tweet_username,
            tweet_id,
        )
        if not tweet_info.get("status", True):
            yield event.plain_result(
                f"无法获取 @{username} 的推文，帖子可能已删除、受限或暂时不可用"
            )
            return
        self._attach_timeline_item_metadata(tweet_info, selected_item)

        translated_text, translate_model = (
            await self.message_service.maybe_translate(tweet_info, umo)
        )
        chain = await self.message_service.build_message_chain(
            username,
            tweet_info,
            translated_text=translated_text,
            translate_model=translate_model,
        )
        if not chain:
            yield event.plain_result(f"未找到 @{username} 的推文内容")
            return

        author_username = str(tweet_info.get("username") or username)
        screen_name = str(
            tweet_info.get("screen_name") or author_username
        )
        nickname = self.message_service.build_author_display(
            author_username,
            screen_name,
        )
        prepared = self.delivery_service.prepare_event_delivery(
            chain,
            nickname,
        )
        if prepared.primary_chain:
            yield event.chain_result(prepared.primary_chain)
        else:
            yield event.plain_result(f"未找到 @{username} 的推文内容")
        await self.delivery_service.send_prepared_videos(
            umo,
            prepared.videos,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """检测 Twitter/X 链接并解析推文。"""
        if not self.link_recognition_enabled:
            return

        umo = event.unified_msg_origin
        match = TWITTER_LINK_PATTERN.search(event.message_str)
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

            translated_text, translate_model = (
                await self.message_service.maybe_translate(tweet_info, umo)
            )
            chain = await self.message_service.build_message_chain(
                username,
                tweet_info,
                {"r18": True, "media": False, "status": True},
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                return

            author_username = str(tweet_info.get("username") or username)
            screen_name = str(
                tweet_info.get("screen_name") or author_username
            )
            nickname = self.message_service.build_author_display(
                author_username,
                screen_name,
            )
            prepared = self.delivery_service.prepare_event_delivery(
                chain,
                nickname,
            )
            if prepared.primary_chain:
                yield event.chain_result(prepared.primary_chain)
            await self.delivery_service.send_prepared_videos(
                umo,
                prepared.videos,
            )
        except Exception as exc:
            logger.error(f"解析推文链接失败: {exc}")
