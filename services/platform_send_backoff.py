"""只重试平台明确拒收的限流请求，不重试结果不确定的网络超时。"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import TypeVar

from astrbot.api import logger

from ..twitter_diagnostics import anchor, reference

T = TypeVar("T")


class PlatformRateLimited(RuntimeError):
    """本次发送暂缓，调用方不得通过拆分消息继续冲击平台。"""


def _seconds(value) -> float | None:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1.0, seconds) if math.isfinite(seconds) else None


def rate_limit_delay(error: Exception, fallback: float) -> float | None:
    """兼容 Telegram RetryAfter、HTTP 429 和保留原始异常的适配器。"""
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        delay = _seconds(getattr(current, "retry_after", None))
        if delay is not None:
            return delay
        response = getattr(current, "response", None)
        status = getattr(current, "status_code", None) or getattr(current, "status", None)
        status = status or getattr(response, "status_code", None) or getattr(response, "status", None)
        if str(status) == "429":
            headers = getattr(response, "headers", None) or getattr(current, "headers", None) or {}
            value = headers.get("Retry-After") or headers.get("retry-after")
            delay = _seconds(value)
            if delay is None and isinstance(value, str):
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)  # noqa: UP017 - Python 3.10。
                    delay = _seconds((date - datetime.now(timezone.utc)).total_seconds())  # noqa: UP017
                except (TypeError, ValueError, OverflowError):
                    pass
            return delay if delay is not None else fallback
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return None


class PlatformSendBackoff:
    """按机器人实例共享冷却；短限流最多重试两次，长限流留待后续轮询。"""

    MAX_RETRIES = 2
    MAX_INLINE_WAIT = 60.0

    def __init__(self) -> None:
        self._deadlines: dict[str, float] = {}

    async def run(self, umo: str, operation: Callable[[], Awaitable[T]]) -> T:
        platform_id = umo.split(":", 1)[0]
        # 单次调用的总等待预算固定，防止连续限流让整个轮询长期停滞。
        wait_budget = self.MAX_INLINE_WAIT
        for attempt in range(self.MAX_RETRIES + 1):
            while True:
                remaining = self._deadlines.get(platform_id, 0.0) - monotonic()
                if remaining <= 0:
                    self._deadlines.pop(platform_id, None)
                    break
                if remaining > wait_budget:
                    logger.debug(anchor(
                        "platform.backoff.deferred", platform=reference(platform_id),
                        retry_after=round(remaining, 2),
                    ))
                    raise PlatformRateLimited("平台仍在限流冷却，保留未完成推送")
                logger.debug(anchor(
                    "platform.backoff.wait", platform=reference(platform_id),
                    seconds=round(remaining, 2),
                ))
                await asyncio.sleep(remaining)
                wait_budget = max(0.0, wait_budget - remaining)
            try:
                return await operation()
            except Exception as exc:
                delay = rate_limit_delay(exc, fallback=2.0 ** (attempt + 1))
                if delay is None:
                    raise
                self._deadlines[platform_id] = max(
                    self._deadlines.get(platform_id, 0.0), monotonic() + delay,
                )
                logger.warning(anchor(
                    "platform.rate_limited", platform=reference(platform_id),
                    target=reference(umo), retry_after=round(delay, 2),
                    attempt=attempt + 1, max_attempts=self.MAX_RETRIES + 1,
                ))
                if attempt == self.MAX_RETRIES:
                    raise PlatformRateLimited("平台限流重试耗尽，保留未完成推送") from exc
        raise AssertionError("unreachable")
