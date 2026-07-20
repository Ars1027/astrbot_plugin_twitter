"""推文消息的拆分、发送、降级和集体转发服务。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node, Nodes

from .subscription_service import SubscriptionService
from .tweet_message_service import TweetMessageService


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
    """轮询层据此决定是否计数、停止或推进游标。"""

    state: DeliveryState

    @property
    def counts_toward_limit(self) -> bool:
        return self.state in {
            DeliveryState.DELIVERED,
            DeliveryState.QUEUED,
        }


@dataclass(frozen=True, slots=True)
class CollectiveFlushResult:
    """集体转发完成后各推主的发送结果。"""

    successful_authors: frozenset[str]
    failed_authors: frozenset[str]


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

    async def send_plain_chain_resilient(self, umo: str, chain: list) -> bool:
        """发送普通消息；媒体失败时优先补发文字，再逐图尝试。"""
        if not chain:
            return True
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))
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
                await self.context.send_message(
                    umo,
                    MessageChain(chain=text_parts),
                )
                text_sent = True
            except Exception as exc:
                logger.error(f"媒体降级后的文字消息仍发送失败: {exc}")

        image_sent = False
        for image_part in image_parts:
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(chain=[image_part]),
                )
                image_sent = True
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
            await self.context.send_message(
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
                    await self.context.send_message(
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
    ) -> DeliveryResult:
        """将推文推送给订阅者，或加入集体转发缓存。"""
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
        retweet_dedup_id = str(tweet_info.get("tweet_id") or "")

        first_umo = next(iter(subscribers), "")
        translated_text, translate_model = await self.messages.maybe_translate(
            tweet_info,
            first_umo,
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
        delivery_failed = False
        retweet_dedup_dirty = False
        for umo, sub_config in subscribers.items():
            if not sub_config.get("status", True):
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
                    retweet_dedup_id,
                )
                pending_seen = retweet_dedup_id in (
                    self._pending_retweet_seen.get(umo) or set()
                )
                if already_seen or pending_seen:
                    logger.debug(
                        f"跳过重复转帖 {umo}: @{username} -> {retweet_dedup_id}"
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
                            retweet_dedup_id if should_dedup_retweet else ""
                        ),
                    )
                )
                if should_dedup_retweet:
                    self._pending_retweet_seen.setdefault(umo, set()).add(
                        retweet_dedup_id
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
            )
            if not sent:
                delivery_failed = True
                continue
            if should_dedup_retweet and retweet_dedup_seen is not None:
                self.subscriptions.mark_retweet_seen(
                    retweet_dedup_seen,
                    umo,
                    retweet_dedup_id,
                )
                retweet_dedup_dirty = True

        if retweet_dedup_dirty and retweet_dedup_seen is not None:
            await self.subscriptions.save_retweet_seen(retweet_dedup_seen)

        if delivery_failed:
            return DeliveryResult(DeliveryState.FAILED)
        if not had_target:
            return DeliveryResult(DeliveryState.SKIPPED)
        if self.collective_enabled:
            return DeliveryResult(DeliveryState.QUEUED)
        return DeliveryResult(DeliveryState.DELIVERED)

    async def send_to_subscriber(
        self,
        umo: str,
        username: str,
        tweet_info: dict,
        sub_config: dict,
        nickname: str,
        translated_text: str | None = None,
        translate_model: str | None = None,
    ) -> bool:
        """向单个订阅者发送推文消息。"""
        try:
            chain = await self.messages.build_message_chain(
                username,
                tweet_info,
                sub_config,
                translated_text=translated_text,
                translate_model=translate_model,
            )
            if not chain:
                return True

            if self.settings.use_node:
                try:
                    nodes, video_parts = self.split_chain_for_nodes(
                        chain,
                        nickname,
                    )
                    if nodes:
                        await self.context.send_message(
                            umo,
                            MessageChain(chain=[Nodes(nodes)]),
                        )
                    video_results = [
                        await self.send_video_or_fallback(umo, video)
                        for video in video_parts
                    ]
                    sent = bool(nodes) or any(video_results)
                except Exception as exc:
                    logger.warning(
                        f"合并转发失败，回退到普通消息: {exc}"
                    )
                    fallback_chain = self.build_plain_chain(chain)
                    sent = bool(fallback_chain) and (
                        await self.send_plain_chain_resilient(
                            umo, fallback_chain
                        )
                    )
            else:
                plain_chain, video_parts = self.split_plain_chain_and_videos(
                    chain
                )
                primary_sent = bool(plain_chain) and (
                    await self.send_plain_chain_resilient(umo, plain_chain)
                )
                video_results = [
                    await self.send_video_or_fallback(umo, video)
                    for video in video_parts
                ]
                sent = primary_sent if plain_chain else any(video_results)

            if sent:
                logger.info(f"推文已推送至 {umo}")
            else:
                logger.error(f"推文主要内容未能推送至 {umo}")
            return sent
        except Exception as exc:
            logger.error(f"推送推文至 {umo} 失败: {exc}")
            return False

    async def flush_collected(self) -> CollectiveFlushResult:
        """发送集体转发缓存，并按推主汇总最终结果。"""
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

        def record_result(
            umo: str,
            cached_tweet: CachedTweet,
            succeeded: bool,
        ) -> None:
            nonlocal retweet_seen_dirty
            if not succeeded:
                author_success[cached_tweet.username] = False
                return
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
                            await self.context.send_message(
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
                            )
                            record_result(umo, cached_tweet, sent)
                        continue

                    for cached_tweet, tweet_nodes, tweet_videos in prepared:
                        video_results = [
                            await self.send_video_or_fallback(umo, video)
                            for video in tweet_videos
                        ]
                        succeeded = bool(tweet_nodes) or any(video_results)
                        record_result(umo, cached_tweet, succeeded)

            if retweet_seen_dirty:
                try:
                    await self.subscriptions.save_retweet_seen(retweet_seen)
                except Exception as exc:
                    logger.error(f"保存集体转发去重记录失败: {exc}")
                    for username in retweet_seen_authors:
                        author_success[username] = False
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
        return CollectiveFlushResult(successful_authors, failed_authors)
