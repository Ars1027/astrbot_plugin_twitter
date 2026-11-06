import asyncio
import re
import sys

import httpx
import pytest
from test_provider_initialization import Plain, Video
from test_telegram_support import TG_GROUP
from test_telegram_support import (
    plugin_env as plugin_env,  # noqa: PLC0414 - 复用插件 fixture。
)


@pytest.mark.asyncio
@pytest.mark.parametrize("link_fails", [False, True])
async def test_video_timeout_diagnostics_preserve_link_fallback(plugin_env, monkeypatch, link_fails):
    _module, make, _data, _ordinary, _fail = plugin_env
    plugin = make()
    delivery = plugin.delivery_service
    module = sys.modules[type(delivery).__module__]
    warnings, errors, sent = [], [], []
    monkeypatch.setattr(module.logger, "warning", warnings.append)
    monkeypatch.setattr(module.logger, "error", errors.append)

    async def send(_umo, message):
        sent.append(message.chain)
        if isinstance(message.chain[0], Video):
            try:
                raise httpx.WriteTimeout("secret URL and token")
            except httpx.WriteTimeout as exc:
                raise TimeoutError("secret wrapper") from exc
        if link_fails:
            raise httpx.ConnectTimeout("secret link URL")
        return True

    plugin.context.send_message = send
    assert await delivery.send_video_or_fallback(
        TG_GROUP, Video(file="https://example.com/video.mp4"),
    ) is not link_fails
    assert len(sent) == 2 and isinstance(sent[1][0], Plain)
    assert sent[1][0].text == "视频: https://example.com/video.mp4"
    assert len(warnings) == 1 and "stage=platform_send" in warnings[0]
    assert "causes=TimeoutError <- WriteTimeout" in warnings[0]
    assert len(errors) == int(link_fails)
    if link_fails:
        assert "stage=send_video_link" in errors[0] and "causes=ConnectTimeout" in errors[0]
    assert "secret" not in "".join(warnings + errors)


def test_exception_chain_handles_hidden_context_and_cycles(plugin_env):
    _module, make, _data, _ordinary, _fail = plugin_env
    delivery = make().delivery_service
    chain = sys.modules[type(delivery).__module__].exception_type_chain
    inner = httpx.ReadTimeout("secret")
    outer = TimeoutError("secret wrapper")
    outer.__context__ = inner
    assert chain(outer) == "TimeoutError <- ReadTimeout"
    outer.__suppress_context__ = True
    assert chain(outer) == "TimeoutError"
    outer.__cause__ = inner
    inner.__cause__ = outer
    assert chain(outer) == "TimeoutError <- ReadTimeout"


@pytest.mark.asyncio
async def test_trace_context_is_nested_task_local_and_restored_after_errors(plugin_env):
    _module, make, _data, _ordinary, _fail = plugin_env
    delivery_module = sys.modules[type(make().delivery_service).__module__]
    diagnostics = sys.modules[delivery_module.anchor.__module__]

    @diagnostics.traced
    async def nested():
        return diagnostics.anchor("nested")

    @diagnostics.traced
    async def operation():
        start = diagnostics.anchor("start")
        await asyncio.sleep(0)
        return start, await nested(), diagnostics.anchor("end")

    results = await asyncio.gather(operation(), operation())
    traces = [{re.search(r"trace=(\w+)", line)[1] for line in lines} for lines in results]
    assert all(len(trace) == 1 for trace in traces) and traces[0] != traces[1]
    assert "trace=-" in diagnostics.anchor("outside")

    @diagnostics.traced
    async def fail(error):
        raise error

    for error in (RuntimeError("failure"), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            await fail(error)
        assert "trace=-" in diagnostics.anchor("outside")


@pytest.mark.asyncio
async def test_poll_logs_link_fallback_before_cursor_commit_with_same_trace(plugin_env, monkeypatch):
    _module, make, data, _ordinary, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_GROUP: {"status": True}}}}
    logs = []
    module = sys.modules[type(plugin.delivery_service).__module__]
    monkeypatch.setattr(module.logger, "debug", logs.append)
    monkeypatch.setattr(module.logger, "warning", logs.append)
    original_send = plugin.context.send_message

    async def timeline(*_args):
        return [{"tweet_id": "101", "username": "tester"}]

    async def detail(*_args):
        return {"tweet_id": "101", "username": "tester", "text": "body"}

    async def build(*_args, **_kwargs):
        return [Plain("body"), Video(file="https://media.test/secret.mp4?token=secret")]

    async def send(umo, message):
        if isinstance(message.chain[0], Video):
            raise httpx.WriteTimeout("secret request")
        return await original_send(umo, message)

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = detail
    plugin.message_service.build_message_chain = build
    plugin.context.send_message = send
    assert await plugin.polling_service.check_user("tester", data["twitter_subs"]["tester"])
    assert data["twitter_subs"]["tester"]["since_id"] == "101"
    anchors = [line for line in logs if "[twitter:" in line]
    assert len({re.search(r"trace=(\w+)", line)[1] for line in anchors}) == 1
    assert next(index for index, line in enumerate(anchors) if "video.link.sent" in line) < next(
        index for index, line in enumerate(anchors) if "cursor.commit.result" in line
    )
    assert any("poll.tweet.result" in line and 'state="delivered"' in line for line in anchors)
    assert "secret" not in "\n".join(anchors)
