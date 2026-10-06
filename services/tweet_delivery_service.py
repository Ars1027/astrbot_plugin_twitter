"""推文消息的拆分、发送、降级和集体转发服务。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node, Nodes

from .subscription_service import DeliveredTweet, RecentDelivery, SubscriptionService
from .tweet_message_service import TranslationCycleState, TweetMessageService


@dataclass(frozen=True, slots=True)
class TweetDeliverySettings:
    """发送过程中不会动态变化的配置。"""

    use_node: bool
    collective_forward: bool
    collective_max_authors: int
    deduplicate_retweets: bool


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    """指令或链接识别要返回的主消息链与独立视频。"""

    primary_chain: list
    videos: list[Comp.Video]


class DeliveryState(Enum):
    """一条推文进入发送流程后的处理状态。"""

    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"
    QUEUED = "queued"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """轮询状态及未落盘的成功会话摘要，由轮询层与游标一起提交。"""

    state: DeliveryState
    recent_deliveries: tuple[RecentDelivery, ...] = ()
    delivered_tweets: tuple[DeliveredTweet, ...] = ()

    @property
    def counts_toward_limit(self) -> bool:
        return self.state in {
            DeliveryState.DELIVERED,
            DeliveryState.QUEUED,
        }


@dataclass(frozen=True, slots=True)
class CollectiveFlushResult:
    """集体转发结果及成功会话摘要；失败推主的摘要也须保留。"""

    successful_authors: frozenset[str]
    failed_authors: frozenset[str]
    recent_deliveries: tuple[RecentDelivery, ...] = ()
    delivered_tweets: tuple[DeliveredTweet, ...] = ()


@dataclass(slots=True)
class CachedTweet:
    """集体转发周期内缓存的一条推文。"""

    username: str
    tweet_info: dict
    sub_config: dict
    nickname: str
    translated_text: str | None = None
    translate_model: str | None = None
    retweet_dedup_id: str = ""


class TweetDeliveryService:
    """统一处理自动推送、直接返回和集体转发的发送差异。"""

    def __init__(
        self,
        context: Any,
        subscriptions: SubscriptionService,
        messages: TweetMessageService,
        settings: TweetDeliverySettings,
    ) -> None:
        self.context = context
        self.subscriptions = subscriptions
        self.messages = messages
        self.settings = settings
        self._collected_tweets: dict[str, list[CachedTweet]] = {}
        self._pending_retweet_seen: dict[str, set[str]] = {}

    @property
    def collective_enabled(self) -> bool:
        return self.settings.collective_forward and self.settings.use_node

    @property
    def has_collected(self) -> bool:
        return bool(self._collected_tweets)

    def clear_collected(self) -> None:
        self._collected_tweets.clear()
        self._pending_retweet_seen.clear()

    @staticmethod
    def split_chain_for_nodes(
        chain: list,
        nickname: str,
    ) -> tuple[list[Node], list[Comp.Video]]:
        """将消息链分离为 Node 列表和待独立发送的视频列表。"""
        nodes: list[Node] = []
        video_parts: list[Comp.Video] = []
        text_parts: list = []

        def flush_text_parts() -> None:
            nonlocal text_parts
            if text_parts:
                nodes.append(Node(content=text_parts, name=nickname))
                text_parts = []

        for component in chain:
            if isinstance(component, Comp.Video):
                video_parts.append(component)
            elif isinstance(component, Comp.Image):
                flush_text_parts()
                nodes.append(Node(content=[component], name=nickname))
            else:
                text_parts.append(component)

        if text_parts:
            nodes.append(Node(content=text_parts, name=nickname))

        return nodes, video_parts

    @staticmethod
    def build_plain_chain(chain: list) -> list:
        """保留图片，并把视频转换为链接文本。"""
        plain_chain = []
        for component in chain:
            if isinstance(component, Comp.Video):
                video_url = getattr(component, "file", "") or getattr(
                    component,
                    "url",
                    "",
                )
                if video_url:
                    plain_chain.append(
                        Comp.Plain(str(f"\n视频: {video_url}"))
                    )
            else:
                plain_chain.append(component)
        return plain_chain

    @staticmethod
    def split_plain_chain_and_videos(
        chain: list,
    ) -> tuple[list, list[Comp.Video]]:
        """构建普通消息链，并分离需要独立发送的视频组件。"""
        plain_chain = []
        video_parts: list[Comp.Video] = []
        for component in chain:
            if isinstance(component, Comp.Video):
                video_parts.append(component)
            else:
                plain_chain.append(component)
        return plain_chain, video_parts

    def prepare_event_delivery(
        self,
        chain: list,
        nickname: str,
    ) -> PreparedDelivery:
        """为测试指令和链接识别准备一致的主消息与视频列表。"""
        if self.settings.use_node:
            try:
                nodes, videos = self.split_chain_for_nodes(chain, nickname)
                primary_chain = [Nodes(nodes)] if nodes else []
                return PreparedDelivery(primary_chain, videos)
            except Exception as exc:
                logger.warning(
                    f"合并转发构建失败，回退到普通消息链: {exc}"
                )

        plain_chain, videos = self.split_plain_chain_and_videos(chain)
        return PreparedDelivery(plain_chain, videos)

    async def _send_message_checked(
        self,
        umo: str,
        message_chain: MessageChain,
    ) -> None:
        """将明确的 False 转为发送异常，保留其他无异常返回的既有行为。"""
        result = await self.context.send_message(umo, message_chain)
        if result is False:
            raise RuntimeError("Context.send_message returned False")

    async def _record_recent_delivery(
        self, umo: str, username: str, tweet_info: dict,
        recent_deliveries: list[RecentDelivery] | None = None,
    ) -> None:
        try:
            if recent_deliveries is None:
                await self.subscriptions.record_delivery(umo, username, tweet_info)
            else:
                record = SubscriptionService.prepare_delivery(umo, username, tweet_info)
                if record is not None:
                    recent_deliveries.append(record)
        except Exception as exc:
            # History is observational; its failure must not trigger a resend.
            logger.warning(f"保存最近推送记录失败 {umo} -> @{username}: {exc}")

    async def send_plain_chain_resilient(
        self, umo: str, chain: list, *, on_primary_sent: Callable[[], None] | None = None
    ) -> bool:
        """发送普通消息；媒体失败时优先补发文字，再逐图尝试。"""
        if not chain:
            return True
        try:
            await self._send_message_checked(umo, MessageChain(chain=chain))
            if on_primary_sent is not None:
                on_primary_sent()
            return True
        except Exception as exc:
            logger.warning(f"包含媒体的消息发送失败，尝试保留文字内容: {exc}")

        text_parts = [
            component
            for component in chain
            if not isinstance(component, Comp.Image)
        ]
        image_parts = [
            component
            for component in chain
            if isinstance(component, Comp.Image)
        ]

        text_sent = False
        if text_parts:
            try:
                await self._send_message_checked(
                    umo,
                    MessageChain(chain=text_parts),
                )
                text_sent = True
                if on_primary_sent is not None:
                    on_primary_sent()
            except Exception as exc:
                logger.error(f"媒体降级后的文字消息仍发送失败: {exc}")

        image_sent = False
        for image_part in image_parts:
            try:
                await self._send_message_checked(
                    umo,
                    MessageChain(chain=[image_part]),
                )
                image_sent = True
                if not text_parts and on_primary_sent is not None:
                    on_primary_sent()
            except Exception as exc:
                image_url = getattr(image_part, "file", "") or getattr(
                    image_part,
                    "url",
                    "",
                )
                logger.warning(
                    f"图片发送失败，已保留文字内容: {image_url}, {exc}"
                )

        if text_parts:
            return text_sent
        return image_sent

    async def send_video_or_fallback(
        self,
        umo: str,
        video_component: Comp.Video,
    ) -> bool:
        """发送视频组件，失败时回退为链接。"""
        try:
            await self._send_message_checked(
                umo,
                MessageChain(chain=[video_component]),
            )
            return True
        except Exception as exc:
            logger.warning(f"视频发送失败，回退为链接: {exc}")
            video_url = getattr(video_component, "file", "") or getattr(
                video_component,
                "url",
                "",
            )
            if video_url:
                try:
                    await self._send_message_checked(
                        umo,
                        MessageChain(
                            chain=[Comp.Plain(str(f"视频: {video_url}"))]
                        ),
                    )
                    return True
                except Exception as fallback_exc:
                    logger.error(
                        f"视频与降级链接均发送失败: {fallback_exc}"
                    )
            return False

    async def send_prepared_videos(
        self,
        umo: str,
        videos: list[Comp.Video],
    ) -> bool:
        """逐条发送准备结果中的独立视频。"""
        results: list[bool] = []
        for video_component in videos:
            results.append(
                await self.send_video_or_fallback(umo, video_component)
            )
        return all(results)

    async def push_to_subscribers(
        self,
        username: str,
        tweet_info: dict,
        cycle: TranslationCycleState | None = None,
    ) -> DeliveryResult:
        """正常返回成功摘要；中断时保存已完成结果，不推进游标。"""
        recent_deliveries: list[RecentDelivery] = []
        delivered_tweets: list[DeliveredTweet] = []
        try:
            return await self._push_to_subscribers(
                username, tweet_info, cycle, recent_deliveries, delivered_tweets
            )
        except (Exception, asyncio.CancelledError):
            await self._save_interrupted_deliveries(delivered_tweets, recent_deliveries)
            raise

    async def _save_interrupted_deliveries(
        self,
        delivered_tweets: list[DeliveredTweet],
        recent_deliveries: list[RecentDelivery],
    ) -> None:
        """异常或取消退出时保存已经确认成功的结果，保留原异常。"""
        if not delivered_tweets and not recent_deliveries:
            return
        try:
            await self.subscriptions.save_pending_deliveries(
                tuple(delivered_tweets), recent_deliveries=tuple(recent_deliveries)
            )
        except Exception as exc:
            logger.warning(f"保存中断前推送结果失败，下次可能重复推送: {exc}")

    async def _push_to_subscribers(
        self,
        username: str,
        tweet_info: dict,
        cycle: TranslationCycleState | None,
        recent_deliveries: list[RecentDelivery],
        delivered_tweets: list[DeliveredTweet],
    ) -> DeliveryResult:
        latest_subs = await self.subscriptions.get_all()
        if username not in latest_subs:
            return DeliveryResult(DeliveryState.SKIPPED)

        latest_user_info = latest_subs[username]
        subscribers = latest_user_info.get("subscribers") or {}
        screen_name = str(
            latest_user_info.get("screen_name")
            or tweet_info.get("screen_name")
            or username
        )
        retweet = tweet_info.get("retweet") or {}
        if retweet:
            nickname = self.messages.build_author_display(
                str(retweet.get("retweeter_username") or username),
                str(retweet.get("retweeter_screen_name") or screen_name),
            )
        else:
            nickname = self.messages.build_nickname(username, screen_name)

        should_dedup_retweet = (
            self.settings.deduplicate_retweets
            and bool(retweet)
            and bool(str(tweet_info.get("tweet_id") or ""))
        )
        retweet_dedup_seen: dict | None = None
        if should_dedup_retweet:
            retweet_dedup_seen = await self.subscriptions.get_retweet_seen()
        tweet_id = str(tweet_info.get("tweet_id") or "")

        first_umo = next(iter(subscribers), "")
        details_available = bool(tweet_info.get("status", True))
        translated_text, translate_model = None, None
        if details_available:
            translated_text, translate_model = await self.messages.maybe_translate(
                tweet_info,
                first_umo,
                cycle=cycle,
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

        had_target = False
        already_delivered = False
        delivery_failed = False
        retweet_dedup_dirty = False
        for umo, sub_config in subscribers.items():
            if tweet_id in (sub_config.get("pending_delivery_ids") or []):
                already_delivered = True
                if should_dedup_retweet and retweet_dedup_seen is not None:
                    self.subscriptions.mark_retweet_seen(retweet_dedup_seen, umo, tweet_id)
                    retweet_dedup_dirty = True
                continue

            if not sub_config.get("status", True):
                continue

            if not details_available:
                delivery_failed = True
                continue

            is_r18 = tweet_info.get("is_r18", False)
            if is_r18 and not sub_config.get("r18", False):
                continue

            if (
                sub_config.get("media", False)
                and not self.messages.tweet_has_media(tweet_info)
            ):
                continue

            if should_dedup_retweet and retweet_dedup_seen is not None:
                already_seen = self.subscriptions.retweet_seen_by_umo(
                    retweet_dedup_seen,
                    umo,
                    tweet_id,
                )
                pending_seen = tweet_id in (
                    self._pending_retweet_seen.get(umo) or set()
                )
                if already_seen or pending_seen:
                    logger.debug(
                        f"跳过重复转帖 {umo}: @{username} -> {tweet_id}"
                    )
                    continue

            had_target = True
            if self.collective_enabled:
                self._collected_tweets.setdefault(umo, []).append(
                    CachedTweet(
                        username=username,
                        tweet_info=tweet_info,
                        sub_config=sub_config,
                        nickname=nickname,
                        translated_text=translated_text,
                        translate_model=translate_model,
                        retweet_dedup_id=(
                            tweet_id if should_dedup_retweet else ""
                        ),
                    )
                )
                if should_dedup_retweet:
                    self._pending_retweet_seen.setdefault(umo, set()).add(
                        tweet_id
                    )
                continue

            sent = await self.send_to_subscriber(
                umo,
                username,
                tweet_info,
                sub_config,
                nickname,
                translated_text=translated_text,
                translate_model=translate_model,
                recent_deliveries=recent_deliveries,
                delivered_tweets=delivered_tweets,
            )
            if not sent:
                delivery_failed = True
                continue
            if should_dedup_retweet and retweet_dedup_seen is not None:
                self.subscriptions.mark_retweet_seen(
                    retweet_dedup_seen,
                    umo,
                    tweet_id,
                )
                retweet_dedup_dirty = True

        if retweet_dedup_dirty and retweet_dedup_seen is not None:
            try:
                await self.subscriptions.save_retweet_seen(retweet_dedup_seen)
            except Exception as exc:
                logger.error(f"保存转帖去重记录失败，保留推送结果等待重试: {exc}")
                delivery_failed = True

        if delivery_failed:
            return DeliveryResult(
                DeliveryState.FAILED, tuple(recent_deliveries), tuple(delivered_tweets)
            )
        if not had_target:
            if already_delivered:
                # 已送达的重试条目仍计入本轮限额，避免失败窗口不断扩大。
                return DeliveryResult(DeliveryState.DELIVERED)
            return DeliveryResult(DeliveryState.SKIPPED)
        if self.collective_enabled:
            return DeliveryResult(DeliveryState.QUEUED)
        return DeliveryResult(
            DeliveryState.DELIVERED, tuple(recent_deliveries), tuple(delivered_tweets)
        )

    async def send_to_subscriber(
        self,
        umo: str,
        username: str,
        tweet_info: dict,
        sub_config: dict,
        nickname: str,
        translated_text: str | None = None,
        translate_model: str | None = None,
        *,
        recent_deliveries: list[RecentDelivery] | None = None,
        delivered_tweets: list[DeliveredTweet] | None = None,
    ) -> bool:
        """向单个订阅者发送推文，成功后尽力记录最近历史。

        轮询传入 recent_deliveries 时只追加内存摘要，由调用方与游标一起
        持久化；直接调用未传入时会写入 KV。历史生成或单独写入失败只记
        告警，不改变发送结果；任务取消仍向上传播。delivered_tweets 在
        主要内容发送成功后追加，附加媒体中断不丢弃正文回执。
        """
        receipt_recorded = False

        def record_sent() -> None:
            nonlocal receipt_recorded
            if delivered_tweets is not None and not receipt_recorded:
                delivered_tweets.append(DeliveredTweet(
                    umo, username, str(tweet_info.get("tweet_id") or "")
                ))
                receipt_recorded = True

        try:
            chain = await self.messages.build_message_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                record_sent()
                return True

            if self.settings.use_node:
                try:
                    nodes, video_parts = self.split_chain_for_nodes(
                        chain,
                        nickname,
                    )
                    if nodes:
                        await self._send_message_checked(
                            umo,
                            MessageChain(chain=[Nodes(nodes)]),
                        )
                        record_sent()
                    video_sent = False
                    for video in video_parts:
                        if await self.send_video_or_fallback(umo, video):
                            video_sent = True
                            if not nodes:
                                record_sent()
                    sent = bool(nodes) or video_sent
                except Exception as exc:
                    logger.warning(
                        f"合并转发失败，回退到普通消息: {exc}"
                    )
                    fallback_chain = self.build_plain_chain(chain)
                    sent = bool(fallback_chain) and (
                        await self.send_plain_chain_resilient(
                            umo, fallback_chain, on_primary_sent=record_sent
                        )
                    )
            else:
                plain_chain, video_parts = self.split_plain_chain_and_videos(
                    chain
                )
                primary_sent = bool(plain_chain) and (
                    await self.send_plain_chain_resilient(umo, plain_chain, on_primary_sent=record_sent)
                )
                video_sent = False
                for video in video_parts:
                    if await self.send_video_or_fallback(umo, video):
                        video_sent = True
                        if not plain_chain:
                            record_sent()
                sent = primary_sent if plain_chain else video_sent

            if sent:
                record_sent()
                await self._record_recent_delivery(umo, username, tweet_info, recent_deliveries)
                logger.info(f"推文已推送至 {umo}")
            else:
                logger.error(f"推文主要内容未能推送至 {umo}")
            return sent
        except Exception as exc:
            logger.error(f"推送推文至 {umo} 失败: {exc}")
            return False

    async def flush_collected(self) -> CollectiveFlushResult:
        """发送集体缓存，返回按推主汇总的结果和待持久化的成功摘要。"""
        if not self._collected_tweets:
            self._pending_retweet_seen.clear()
            return CollectiveFlushResult(frozenset(), frozenset())

        collected = self._collected_tweets
        self._collected_tweets = {}
        latest_subs = await self.subscriptions.get_all()
        author_success = {
            cached_tweet.username: True
            for cached_list in collected.values()
            for cached_tweet in cached_list
        }
        retweet_seen = await self.subscriptions.get_retweet_seen()
        retweet_seen_dirty = False
        retweet_seen_authors: set[str] = set()
        recent_deliveries: list[RecentDelivery] = []
        delivered_tweets: list[DeliveredTweet] = []

        def record_result(
            umo: str,
            cached_tweet: CachedTweet,
            succeeded: bool,
        ) -> None:
            nonlocal retweet_seen_dirty
            if not succeeded:
                author_success[cached_tweet.username] = False
                return
            receipt = DeliveredTweet(
                umo, cached_tweet.username, str(cached_tweet.tweet_info.get("tweet_id") or "")
            )
            if receipt not in delivered_tweets:
                delivered_tweets.append(receipt)
            if cached_tweet.retweet_dedup_id:
                self.subscriptions.mark_retweet_seen(
                    retweet_seen,
                    umo,
                    cached_tweet.retweet_dedup_id,
                )
                retweet_seen_dirty = True
                retweet_seen_authors.add(cached_tweet.username)

        try:
            for umo, cached_list in collected.items():
                if not cached_list:
                    continue

                valid_tweets: list[CachedTweet] = []
                for cached_tweet in cached_list:
                    user_info = latest_subs.get(cached_tweet.username)
                    subscribers = (
                        user_info.get("subscribers", {})
                        if isinstance(user_info, dict)
                        else {}
                    )
                    sub_config = subscribers.get(umo)
                    if isinstance(sub_config, dict) and sub_config.get(
                        "status", True
                    ):
                        cached_tweet.sub_config = sub_config
                        valid_tweets.append(cached_tweet)
                    elif sub_config is not None:
                        logger.debug(
                            "集体转发跳过已暂停的订阅: "
                            f"{umo} -> @{cached_tweet.username}"
                        )
                    else:
                        logger.debug(
                            "集体转发跳过已取关的订阅: "
                            f"{umo} -> @{cached_tweet.username}"
                        )

                if not valid_tweets:
                    continue

                tweets_by_author: dict[str, list[CachedTweet]] = {}
                author_order: list[str] = []
                for cached_tweet in valid_tweets:
                    if cached_tweet.username not in tweets_by_author:
                        tweets_by_author[cached_tweet.username] = []
                        author_order.append(cached_tweet.username)
                    tweets_by_author[cached_tweet.username].append(cached_tweet)

                max_authors = self.settings.collective_max_authors
                author_batches = [
                    author_order[index : index + max_authors]
                    for index in range(0, len(author_order), max_authors)
                ]

                for batch_index, batch_authors in enumerate(author_batches):
                    batch_tweets = [
                        item
                        for author in batch_authors
                        for item in tweets_by_author[author]
                    ]
                    prepared: list[
                        tuple[CachedTweet, list[Node], list[Comp.Video]]
                    ] = []
                    nodes: list[Node] = []

                    for cached_tweet in batch_tweets:
                        try:
                            chain = await self.messages.build_message_chain(
                                cached_tweet.username,
                                cached_tweet.tweet_info,
                                cached_tweet.sub_config,
                                translated_text=cached_tweet.translated_text,
                                translate_model=cached_tweet.translate_model,
                            )
                            if not chain:
                                record_result(umo, cached_tweet, True)
                                continue
                            tweet_nodes, tweet_videos = (
                                self.split_chain_for_nodes(
                                    chain,
                                    cached_tweet.nickname,
                                )
                            )
                            prepared.append(
                                (cached_tweet, tweet_nodes, tweet_videos)
                            )
                            nodes.extend(tweet_nodes)
                        except Exception as exc:
                            logger.error(
                                "构建集体转发推文失败: "
                                f"{umo} -> @{cached_tweet.username}, {exc}"
                            )
                            record_result(umo, cached_tweet, False)

                    nodes_sent = False
                    if nodes:
                        batch_label = ""
                        if len(author_batches) > 1:
                            batch_label = (
                                f"（第{batch_index + 1}/{len(author_batches)}批）"
                            )
                        try:
                            await self._send_message_checked(
                                umo,
                                MessageChain(chain=[Nodes(nodes)]),
                            )
                            nodes_sent = True
                            logger.info(
                                f"集体转发已推送至 {umo} "
                                f"{batch_label}共 {len(nodes)} 个节点"
                            )
                        except Exception as exc:
                            logger.warning(
                                f"集体合并转发失败，回退逐条发送: {exc}"
                            )

                    if nodes and not nodes_sent:
                        for cached_tweet, _tweet_nodes, _tweet_videos in prepared:
                            sent = await self.send_to_subscriber(
                                umo,
                                cached_tweet.username,
                                cached_tweet.tweet_info,
                                cached_tweet.sub_config,
                                cached_tweet.nickname,
                                translated_text=cached_tweet.translated_text,
                                translate_model=cached_tweet.translate_model,
                                recent_deliveries=recent_deliveries,
                                delivered_tweets=delivered_tweets,
                            )
                            record_result(umo, cached_tweet, sent)
                        continue

                    # 整批节点均已发送，先保留全部正文回执再等待附加媒体。
                    for cached_tweet, tweet_nodes, _tweet_videos in prepared:
                        if tweet_nodes:
                            delivered_tweets.append(DeliveredTweet(
                                umo, cached_tweet.username,
                                str(cached_tweet.tweet_info.get("tweet_id") or ""),
                            ))

                    for cached_tweet, tweet_nodes, tweet_videos in prepared:
                        succeeded = bool(tweet_nodes)
                        if succeeded:
                            record_result(umo, cached_tweet, True)
                            await self._record_recent_delivery(
                                umo, cached_tweet.username, cached_tweet.tweet_info,
                                recent_deliveries,
                            )
                        for video in tweet_videos:
                            if await self.send_video_or_fallback(umo, video) and not succeeded:
                                succeeded = True
                                record_result(umo, cached_tweet, True)
                                await self._record_recent_delivery(
                                    umo, cached_tweet.username, cached_tweet.tweet_info,
                                    recent_deliveries,
                                )
                        if not succeeded:
                            record_result(umo, cached_tweet, False)

            if retweet_seen_dirty:
                try:
                    await self.subscriptions.save_retweet_seen(retweet_seen)
                except Exception as exc:
                    logger.error(f"保存集体转发去重记录失败: {exc}")
                    for username in retweet_seen_authors:
                        author_success[username] = False
        except (Exception, asyncio.CancelledError):
            await self._save_interrupted_deliveries(delivered_tweets, recent_deliveries)
            raise
        finally:
            self._pending_retweet_seen.clear()

        successful_authors = frozenset(
            username
            for username, succeeded in author_success.items()
            if succeeded
        )
        failed_authors = frozenset(
            username
            for username, succeeded in author_success.items()
            if not succeeded
        )
        return CollectiveFlushResult(
            successful_authors, failed_authors, tuple(recent_deliveries), tuple(delivered_tweets)
        )
