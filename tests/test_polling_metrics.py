import asyncio
import json
import re
import sys
import types
from dataclasses import replace

import pytest
from test_provider_initialization import Plain
from test_telegram_support import TG_GROUP
from test_telegram_support import plugin_env as plugin_env  # noqa: PLC0414


@pytest.fixture
def round_env(plugin_env, monkeypatch):
    _module, make, data, calls, fail = plugin_env
    plugin = make()
    module = sys.modules[type(plugin.polling_service).__module__]
    clock = types.SimpleNamespace(now=0.0, logs=[])

    async def sleep(seconds):
        clock.now += seconds

    async def build(*_args, **_kwargs):
        return [Plain("body")]

    async def detail(username, tweet_id):
        return {"tweet_id": tweet_id, "username": username, "text": "body"}

    monkeypatch.setattr(module, "asyncio", types.SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(module, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(module.logger, "info", clock.logs.append)
    plugin.twitter_api.get_tweet = detail
    plugin.message_service.build_message_chain = build
    return plugin, data, calls, fail, clock


def summary(clock):
    log = next(line for line in reversed(clock.logs) if "[twitter:poll.round.end]" in line)
    fields = re.findall(r'(\w+)=("[^"]*"|null|true|false|[\d.]+)(?:\s|$)', log)
    return {key: json.loads(value) for key, value in fields}


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_round_counts_known_backlog_after_actual_flush_and_waits(round_env, collective):
    plugin, data, _calls, _fail, clock = round_env
    plugin.delivery_service.settings = replace(plugin.delivery_service.settings, collective_forward=collective)
    plugin.polling_service.settings = replace(plugin.polling_service.settings, max_tweets_per_user=1)
    data["twitter_subs"] = {
        author: {"since_id": "100", "subscribers": {TG_GROUP: {}}}
        for author in ("alice", "bob")
    }

    async def timeline(username, _since):
        return [{"tweet_id": str(i), "username": username} for i in range(101, 104 if username == "alice" else 102)]

    plugin.twitter_api.get_user_timeline_items = timeline
    await plugin.polling_service.check_all()
    result = summary(clock)
    assert result == {
        "status": "completed", "elapsed": 6.0, "authors": 2, "checked": 2,
        "unchecked": 0, "checks_failed": 0, "delivery_failures": 0,
        "limit_authors": 1, "known_backlog": 2, "backlog_authors": 1,
    }
    assert all(info["since_id"] == "101" for info in data["twitter_subs"].values())
    # 数据源不再返回条目时，仍从持久化窗口处理积压。
    async def empty(*_args):
        return []
    plugin.twitter_api.get_user_timeline_items = empty
    await plugin.polling_service.check_all()
    assert data["twitter_subs"]["alice"]["since_id"] == "102"
    assert summary(clock)["known_backlog"] == 1
    await plugin.polling_service.check_all()
    assert data["twitter_subs"]["alice"]["since_id"] == "103"
    assert summary(clock)["known_backlog"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_limited_window_survives_reload_and_missing_timeline(plugin_env, collective):
    _module, make, data, calls, _fail = plugin_env
    plugin = make(collective=collective)
    plugin.polling_service.settings = replace(plugin.polling_service.settings, max_tweets_per_user=1)
    data["twitter_subs"] = {"alice": {"since_id": "100", "subscribers": {TG_GROUP: {}}}}
    items = [
        {"tweet_id": str(i), "username": "original", "is_retweet": True,
         "retweeter_username": "alice", "retweeter_screen_name": "Alice"}
        for i in (101, 102, 103)
    ]

    async def timeline(*_args):
        return items

    details = []

    async def detail(username, tweet_id):
        details.append((username, tweet_id))
        return {"tweet_id": tweet_id, "username": username, "text": tweet_id}

    async def build(*_args, **_kwargs):
        return [Plain("body")]

    async def round_for(instance):
        instance.twitter_api.get_tweet = detail
        instance.message_service.build_message_chain = build
        info = await instance.subscription_service.get_all()
        await instance.polling_service.check_user("alice", info["alice"])
        await instance.polling_service.flush_pending_collective()

    plugin.twitter_api.get_user_timeline_items = timeline
    await round_for(plugin)
    author = data["twitter_subs"]["alice"]
    assert author["since_id"] == "101"
    assert [item["tweet_id"] for item in author["pending_tweet_items"]] == ["102", "103"]
    assert author["pending_tweet_items"][0]["is_retweet"]

    async def unavailable(*_args):
        pytest.fail("窗口未完成时不应依赖时间线重新发现剩余条目")

    # 每轮都重建插件，证明恢复不依赖上轮的内存缓存。
    for cursor in ("102", "103"):
        reloaded = make(collective=collective)
        reloaded.polling_service.settings = replace(reloaded.polling_service.settings, max_tweets_per_user=1)
        reloaded.twitter_api.get_user_timeline_items = unavailable
        await round_for(reloaded)
        assert data["twitter_subs"]["alice"]["since_id"] == cursor
    assert details == [("original", str(i)) for i in (101, 102, 103)]
    assert len(calls) == 3
    assert "pending_tweet_items" not in data["twitter_subs"]["alice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_failed_delivery_remains_backlogged_with_confirmed_recipients_deduplicated(round_env, collective):
    plugin, data, _calls, fail, clock = round_env
    plugin.delivery_service.settings = replace(plugin.delivery_service.settings, collective_forward=collective)
    data["twitter_subs"] = {"alice": {"since_id": "100", "subscribers": {TG_GROUP: {}, "qq-bot:GroupMessage:1": {}}}}
    fail.add(TG_GROUP)

    async def timeline(*_args):
        return [{"tweet_id": "101", "username": "alice"}, {"tweet_id": "102", "username": "alice"}]

    plugin.twitter_api.get_user_timeline_items = timeline
    await plugin.polling_service.check_all()
    result = summary(clock)
    assert result["known_backlog"] == 2 and result["backlog_authors"] == 1
    assert data["twitter_subs"]["alice"]["since_id"] == "100"
    fail.clear()
    await plugin.polling_service.check_all()
    assert summary(clock)["known_backlog"] == 0
    assert data["twitter_subs"]["alice"]["since_id"] == "102"


@pytest.mark.asyncio
async def test_early_provider_failure_reports_unchecked_authors_and_existing_backlog(round_env):
    plugin, data, _calls, _fail, clock = round_env
    data["twitter_subs"] = {
        "alice": {"since_id": "100", "subscribers": {TG_GROUP: {}}},
        "bob": {"since_id": "100", "pending_tweet_items": [{"tweet_id": "101"}],
                "subscribers": {TG_GROUP: {"pending_delivery_ids": ["101"]}}},
    }

    async def timeline(*_args):
        plugin.twitter_api.provider_ready = False
        raise RuntimeError("provider unavailable")

    plugin.twitter_api.get_user_timeline_items = timeline
    await plugin.polling_service.check_all()
    result = summary(clock)
    assert result["status"] == "stopped_early"
    assert result["checked"] == result["unchecked"] == result["checks_failed"] == 1
    assert result["known_backlog"] == result["backlog_authors"] == 1


@pytest.mark.asyncio
async def test_cancellation_reports_interrupted_round_and_keeps_retry_window(round_env):
    plugin, data, _calls, _fail, clock = round_env
    data["twitter_subs"] = {"alice": {"since_id": "100", "subscribers": {TG_GROUP: {}}}}

    async def timeline(*_args):
        return [{"tweet_id": "101", "username": "alice"}]

    async def detail(*_args):
        raise asyncio.CancelledError()

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = detail
    with pytest.raises(asyncio.CancelledError):
        await plugin.polling_service.check_all()
    assert summary(clock)["status"] == "interrupted"
    assert summary(clock)["known_backlog"] == 1
    assert data["twitter_subs"]["alice"]["pending_tweet_items"][0]["tweet_id"] == "101"


@pytest.mark.asyncio
async def test_empty_round_has_zero_backlog(round_env):
    plugin, _data, _calls, _fail, clock = round_env
    await plugin.polling_service.check_all()
    assert summary(clock)["authors"] == summary(clock)["known_backlog"] == 0
