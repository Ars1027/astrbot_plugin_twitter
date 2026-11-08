import asyncio
import sys
import types
from datetime import timedelta

import httpx
import pytest
from test_provider_initialization import Image, Plain, Video
from test_telegram_albums import albums as albums  # noqa: PLC0414
from test_telegram_support import TG_GROUP, TG_PRIVATE, TG_TOPIC
from test_telegram_support import plugin_env as plugin_env  # noqa: PLC0414


class RetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__("secret URL and token")
        self.retry_after = seconds


def clock_for(plugin, monkeypatch):
    module = sys.modules[type(plugin.delivery_service.send_backoff).__module__]
    clock = types.SimpleNamespace(now=100.0, sleeps=[], logs=[])

    async def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module, "asyncio", types.SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(module.logger, "warning", clock.logs.append)
    return clock, module


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [3, timedelta(seconds=3)])
async def test_sdk_retry_after_waits_and_retries_original_message(plugin_env, monkeypatch, delay):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    sent = []

    async def send(umo, message):
        sent.append((umo, message))
        if len(sent) == 1:
            raise RetryAfter(delay)
        return True

    plugin.context.send_message = send
    assert await plugin.delivery_service.send_plain_chain_resilient(TG_GROUP, [Plain("body")])
    assert len(sent) == 2 and sent[0][1] is sent[1][1]
    assert clock.sleeps == [3]
    assert "platform.rate_limited" in clock.logs[0]
    assert "secret" not in "\n".join(clock.logs)


@pytest.mark.asyncio
async def test_telegram_limit_on_later_component_does_not_replay_successful_prefix(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    delivered = []
    attempts = []
    limited = False

    async def send(_umo, message):
        nonlocal limited
        for part in message.chain:
            attempts.append(part.text)
            if part.text == "link" and not limited:
                limited = True
                raise RetryAfter(3)
            delivered.append(part.text)
        return True

    plugin.context.send_message = send
    assert await plugin.delivery_service.send_plain_chain_resilient(TG_TOPIC, [Plain("body"), Plain("link")])
    assert delivered == ["body", "link"]
    assert attempts == ["body", "link", "link"]
    assert clock.sleeps == [3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["a" * 9000, "🌿" * 4500, "a" * 4095 + "🌿" + "末尾"],
    ids=["long-ascii", "long-emoji", "utf16-boundary"],
)
async def test_telegram_long_text_is_split_before_retry_boundary(plugin_env, monkeypatch, text):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    delivered = []
    targets = []
    limited = False

    async def send(umo, message):
        nonlocal limited
        assert len(message.chain) == 1
        chunk = message.chain[0].text
        # 若整篇交给适配器拆分，第二段失败时会重发第一段。
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
        if delivered and not limited:
            limited = True
            raise RetryAfter(2)
        targets.append(umo)
        delivered.append(chunk)
        return True

    plugin.context.send_message = send
    assert await plugin.delivery_service.send_plain_chain_resilient(TG_TOPIC, [Plain(text)])
    assert "".join(delivered) == text
    assert targets == [TG_TOPIC] * len(delivered)
    assert clock.sleeps == [2]


@pytest.mark.asyncio
async def test_telegram_limited_media_retries_only_that_component(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    image = Image(file="photo.png")
    delivered = []
    limited = False

    async def send(_umo, message):
        nonlocal limited
        for part in message.chain:
            if part is image and not limited:
                limited = True
                raise RetryAfter(1)
            delivered.append(part)
        return True

    plugin.context.send_message = send
    assert await plugin.delivery_service.send_plain_chain_resilient(TG_GROUP, [Plain("body"), image])
    assert len(delivered) == 2 and delivered[0].text == "body" and delivered[1] is image
    assert clock.sleeps == [1]


@pytest.mark.asyncio
async def test_long_limit_is_shared_by_bot_across_topics_media_and_chats(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    calls = []

    async def send(umo, message):
        calls.append((umo, message))
        if len(calls) == 1:
            raise RetryAfter(120)
        return True

    plugin.context.send_message = send
    delivery = plugin.delivery_service
    assert not await delivery.send_plain_chain_resilient(TG_TOPIC, [Plain("body"), Image(file="a")])
    assert not await delivery.send_video_or_fallback(TG_PRIVATE, Video(file="https://video.test/a"))
    assert not await delivery.send_plain_chain_resilient(TG_GROUP, [Plain("other chat")])
    assert len(calls) == 1 and not clock.sleeps
    # 另一机器人实例不受该冷却影响。
    assert await delivery.send_plain_chain_resilient("qq-bot:GroupMessage:1", [Plain("QQ")])
    clock.now += 120
    assert await delivery.send_plain_chain_resilient(TG_GROUP, [Plain("recovered")])
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_http_429_without_retry_after_uses_bounded_exponential_backoff(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, module = clock_for(plugin, monkeypatch)
    attempts = []
    response = httpx.Response(429, request=httpx.Request("POST", "https://secret.test/token"))

    async def send():
        attempts.append(clock.now)
        raise httpx.HTTPStatusError("secret", request=response.request, response=response)

    with pytest.raises(module.PlatformRateLimited):
        await plugin.delivery_service.send_backoff.run(TG_GROUP, send)
    assert attempts == [100, 102, 106]
    assert clock.sleeps == [2, 4]
    assert len(clock.logs) == 3 and "secret" not in "\n".join(clock.logs)


@pytest.mark.asyncio
async def test_repeated_limits_share_a_total_wait_budget(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, module = clock_for(plugin, monkeypatch)
    attempts = []

    async def send():
        attempts.append(clock.now)
        raise RetryAfter(40)

    with pytest.raises(module.PlatformRateLimited):
        await plugin.delivery_service.send_backoff.run(TG_GROUP, send)
    assert attempts == [100, 140] and clock.sleeps == [40]


def test_structured_429_headers_and_nested_sdk_errors(plugin_env):
    plugin = plugin_env[1]()
    module = sys.modules[type(plugin.delivery_service.send_backoff).__module__]
    response = httpx.Response(429, headers={"Retry-After": "7"}, request=httpx.Request("POST", "https://test"))
    error = httpx.HTTPStatusError("failure", request=response.request, response=response)
    assert module.rate_limit_delay(error, 2) == 7
    outer = RuntimeError("wrapped")
    outer.__cause__ = RetryAfter(timedelta(seconds=9))
    assert module.rate_limit_delay(outer, 2) == 9
    outer.__cause__.__cause__ = outer
    assert module.rate_limit_delay(TimeoutError("429 in arbitrary text"), 2) is None
    assert module.rate_limit_delay(RuntimeError("Too many requests"), 2) is None
    assert module.rate_limit_delay(RetryAfter(float("nan")), 2) is None
    error = RuntimeError("429")
    error.status_code = 429
    error.headers = None
    assert module.rate_limit_delay(error, 2) == 2


@pytest.mark.asyncio
async def test_network_timeout_is_not_retried_and_cancellation_is_not_swallowed(plugin_env, monkeypatch):
    plugin = plugin_env[1]()
    clock, _module = clock_for(plugin, monkeypatch)
    attempts = []

    async def send(error):
        attempts.append(error)
        raise error

    for error in (httpx.WriteTimeout("uncertain result"), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            await plugin.delivery_service.send_backoff.run(TG_GROUP, lambda error=error: send(error))
    assert len(attempts) == 2 and not clock.sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_album_retry_rewinds_uploads_and_never_falls_back_on_limit(albums, monkeypatch, count):
    plugin, _data, ordinary, _sent, _converted, client, chain = albums
    clock, _module = clock_for(plugin, monkeypatch)
    uploads = []
    limited = 3

    async def send(**payload):
        files = [part.media for part in payload["media"]] if "media" in payload else [payload["photo"]]
        uploads.append([file.read() for file in files])
        if len(uploads) <= limited:
            raise RetryAfter(1)
        return [object() for _ in files] if "media" in payload else object()

    client.send_photo = client.send_media_group = send
    images = [Image(file=f"a{i}.png") for i in range(count)]
    assert not await plugin.delivery_service.send_telegram_tweet(TG_GROUP, chain(images), "tester", {"tweet_id": "101"})
    assert uploads == [[b"image"] * count] * 3
    assert not ordinary
    limited = 0
    assert await plugin.delivery_service.send_telegram_tweet(TG_GROUP, chain(images), "tester", {"tweet_id": "101"})
    assert uploads[-1] == [b"image"] * count
    assert clock.sleeps == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
@pytest.mark.parametrize("umo", [TG_GROUP, "qq-bot:GroupMessage:1"])
async def test_long_limit_preserves_cursor_and_successful_other_bot_receipts(plugin_env, monkeypatch, collective, umo):
    _module, make, data, _ordinary, _fail = plugin_env
    plugin = make(collective=collective)
    clock, _backoff_module = clock_for(plugin, monkeypatch)
    other = "qq-bot:GroupMessage:2" if umo == TG_GROUP else TG_GROUP
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {umo: {}, other: {}}}}
    calls = []
    limited = True

    async def timeline(*_args):
        return [{"tweet_id": "101", "username": "tester"}]

    async def detail(*_args):
        return {"tweet_id": "101", "username": "tester", "text": "body"}

    async def build(*_args, **_kwargs):
        return [Plain("body")]

    async def send(target, _chain):
        calls.append(target)
        if target == umo and limited:
            raise RetryAfter(120)
        return True

    async def no_sleep(_seconds):
        pass

    polling_module = sys.modules[type(plugin.polling_service).__module__]
    monkeypatch.setattr(polling_module, "asyncio", types.SimpleNamespace(sleep=no_sleep, CancelledError=asyncio.CancelledError))
    plugin.context.send_message = send
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = detail
    plugin.message_service.build_message_chain = build
    await plugin.polling_service.check_all()
    author = data["twitter_subs"]["tester"]
    assert author["since_id"] == "100"
    assert author["subscribers"][other]["pending_delivery_ids"] == ["101"]
    assert calls == [umo, other]
    assert author["pending_tweet_items"][0]["tweet_id"] == "101"
    clock.now += 120
    limited = False
    calls.clear()
    await plugin.polling_service.check_all()
    assert calls == [umo]
    assert data["twitter_subs"]["tester"]["since_id"] == "101"
