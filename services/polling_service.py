"""单轮订阅检查、游标推进和 Nitter 镜像切换。"""

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..twitter_api import (
    DATA_PROVIDER_NITTER,
    FxTwitterTimelineError,
    get_next_website,
)
from .subscription_service import SubscriptionService
from .tweet_delivery_service import (
    DeliveryResult,
    DeliveryState,
    TweetDeliveryService,
)


@dataclass(frozen=True, slots=True)
class PollingSettings:
    """执行轮询检查时使用的数据源与过滤配置。"""

    include_retweets: bool
    data_provider: str
    custom_nitter_url: str
    website_list: tuple[str, ...]
    max_tweets_per_user: int = 5


class PollingService:
    """执行一轮完整轮询，不管理后台任务生命周期。"""

    def __init__(
        self,
        twitter_api: Any,
        subscriptions: SubscriptionService,
        delivery: TweetDeliveryService,
        settings: PollingSettings,
    ) -> None:
        self.twitter_api = twitter_api
        self.subscriptions = subscriptions
        self.delivery = delivery
        self.settings = settings
        self._pending_collective_cursors: dict[str, str] = {}
        self._pending_collective_tweet_ids: dict[str, list[str]] = {}

    @property
    def has_pending_collective(self) -> bool:
        return bool(
            self._pending_collective_cursors
            or self._pending_collective_tweet_ids
        )

    def _set_pending_collective_cursor(
        self,
        username: str,
        tweet_id: str,
    ) -> None:
        """记录本轮集体转发成功后可以提交的最远游标。"""
        pending_ids = self._pending_collective_tweet_ids.setdefault(
            username,
            [],
        )
        if tweet_id not in pending_ids:
            pending_ids.append(tweet_id)
        current_id = self._pending_collective_cursors.get(username, "")
        if not current_id or int(tweet_id) > int(current_id):
            self._pending_collective_cursors[username] = tweet_id

    async def _record_processed_cursor(
        self,
        username: str,
        tweet_id: str,
    ) -> None:
        """普通模式立即落盘，集体转发模式延迟到实际发送后。"""
        if getattr(self.delivery, "collective_enabled", False):
            self._set_pending_collective_cursor(username, tweet_id)
            return
        await self.subscriptions.commit_processed_tweets(
            username,
            [tweet_id],
            tweet_id,
        )

    async def flush_pending_collective(self) -> None:
        """发送集体转发缓存，并只提交发送成功推主的候选游标。"""
        if not self.delivery.collective_enabled:
            return
        if not self.delivery.has_collected and not self._pending_collective_cursors:
            return

        pending_cursors = self._pending_collective_cursors
        pending_tweet_ids = self._pending_collective_tweet_ids
        self._pending_collective_cursors = {}
        self._pending_collective_tweet_ids = {}
        try:
            flush_result = await self.delivery.flush_collected()
        except Exception as exc:
            logger.error(f"集体转发刷新失败，保留全部推主游标: {exc}")
            self.delivery.clear_collected()
            return

        for username, tweet_id in pending_cursors.items():
            if username in flush_result.failed_authors:
                logger.warning(
                    f"@{username} 集体转发未全部成功，保留当前游标"
                )
                continue
            await self.subscriptions.commit_processed_tweets(
                username,
                pending_tweet_ids.get(username, []),
                tweet_id,
            )

    @staticmethod
    def attach_timeline_item_metadata(tweet_info: dict, item: dict) -> None:
        """把时间线条目上的转帖元数据补到推文详情里。"""
        tweet_info["username"] = str(
            item.get("username") or tweet_info.get("username") or ""
        )
        if item.get("is_retweet"):
            tweet_info["retweet"] = {
                "retweeter_username": str(
                    item.get("retweeter_username") or ""
                ),
                "retweeter_screen_name": str(
                    item.get("retweeter_screen_name") or ""
                ),
            }
        else:
            tweet_info["retweet"] = None

    async def check_all(self) -> None:
        """检查全部已订阅推主的一轮新推文。"""
        subscribe_list = await self.subscriptions.get_all()
        if not subscribe_list:
            return

        results: list[bool] = []
        for username, info in subscribe_list.items():
            try:
                result = await self.check_user(username, info)
                results.append(result)
                await asyncio.sleep(3)
            except Exception as exc:
                logger.error(f"检查 {username} 推文失败: {exc}")
                results.append(False)

        if self.delivery.collective_enabled:
            await self.flush_pending_collective()

        if (
            self.settings.data_provider == DATA_PROVIDER_NITTER
            and not self.settings.custom_nitter_url
            and results
        ):
            success_count = sum(1 for result in results if result)
            if (
                success_count < len(results) / 2
                and self.settings.website_list
            ):
                new_url = get_next_website(
                    list(self.settings.website_list),
                    self.twitter_api.nitter_url,
                )
                if new_url and new_url != self.twitter_api.nitter_url:
                    logger.info(f"当前镜像站出错过多，切换至: {new_url}")
                    self.twitter_api.nitter_url = new_url

    async def check_user(self, username: str, info: dict) -> bool:
        """检查一个推主的新推文，并仅推进已成功处理的游标。"""
        try:
            since_id = info.get("since_id", "")
            processed_tweet_ids = self.subscriptions.processed_tweet_ids(info)
            new_tweet_items = await self.twitter_api.get_user_timeline_items(
                username,
                since_id,
            )

            if not new_tweet_items:
                return True

            latest_subs = await self.subscriptions.get_all()
            latest_key = self.subscriptions.find_key(latest_subs, username)
            if latest_key is None:
                logger.info(f"@{username} 已无订阅者，跳过推送")
                return True
            max_tweets = max(1, int(self.settings.max_tweets_per_user))
            pushed_count = 0
            detail_failed = False
            for item_index, item in enumerate(new_tweet_items):
                if pushed_count >= max_tweets:
                    remaining = len(new_tweet_items) - item_index
                    logger.info(
                        f"@{username} 本轮已处理 {max_tweets} 条推文，"
                        f"剩余约 {remaining} 条将在后续轮询继续"
                    )
                    break

                tweet_id = str(item.get("tweet_id") or "")
                tweet_username = str(item.get("username") or username)
                if not tweet_id.isdigit():
                    continue

                if tweet_id in processed_tweet_ids:
                    logger.info(
                        f"跳过 @{username} 已处理的重复推文: {tweet_id}"
                    )
                    await self._record_processed_cursor(username, tweet_id)
                    continue

                if (
                    item.get("is_retweet")
                    and not self.settings.include_retweets
                ):
                    logger.debug(f"跳过 @{username} 转帖: {tweet_id}")
                    await self._record_processed_cursor(username, tweet_id)
                    processed_tweet_ids.add(tweet_id)
                    continue

                tweet_info = await self.twitter_api.get_tweet(
                    tweet_username,
                    tweet_id,
                )
                if not tweet_info.get("status", True):
                    logger.warning(
                        f"获取 @{username} 推文详情失败，保留游标等待下次重试: "
                        f"{tweet_id}"
                    )
                    detail_failed = True
                    break

                self.attach_timeline_item_metadata(tweet_info, item)
                delivery_result = await self.delivery.push_to_subscribers(
                    username,
                    tweet_info,
                )
                if not isinstance(delivery_result, DeliveryResult):
                    logger.error(
                        f"@{username} 推送服务返回了无效结果，保留游标: "
                        f"{tweet_id}"
                    )
                    break
                if delivery_result.state is DeliveryState.FAILED:
                    logger.warning(
                        f"@{username} 推文发送失败，保留游标等待重试: "
                        f"{tweet_id}"
                    )
                    break

                await self._record_processed_cursor(username, tweet_id)
                processed_tweet_ids.add(tweet_id)
                if delivery_result.counts_toward_limit:
                    pushed_count += 1

            return not detail_failed
        except FxTwitterTimelineError as exc:
            logger.warning(
                f"获取 @{username} 时间线失败，保留当前游标: {exc}"
            )
            return False
        except Exception as exc:
            logger.error(f"获取 {username} 推文异常: {exc}")
            return False
