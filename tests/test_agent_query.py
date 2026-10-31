import copy
import importlib.util
import types
from pathlib import Path

import pytest

from test_provider_initialization import _load_main_module


spec = importlib.util.spec_from_file_location(
    "agent_query_subscription_service",
    Path(__file__).resolve().parents[1] / "services/subscription_service.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_format_recent_deliveries_for_agent_empty_and_rich():
    assert module.SubscriptionService.format_recent_deliveries_for_agent([]) == (
        "本会话暂无成功推送摘要。"
    )
    text = module.SubscriptionService.format_recent_deliveries_for_agent([{
        "username": "tester",
        "tweet_id": "1",
        "text": "你好",
        "truncated": True,
        "delivered_at": "2026-10-30T12:00:00+00:00",
        "is_retweet": True,
        "has_media": True,
        "url": "https://x.com/tester/status/1",
    }])
    assert "1. @tester（转帖/含媒体）" in text
    assert "你好…" in text
    assert "https://x.com/tester/status/1" in text


@pytest.mark.asyncio
async def test_twitter_recent_pushes_tool_respects_switch_and_session():
    data = {"twitter_subs": {
        "tester": {
            "since_id": "1",
            "subscribers": {
                "session-a": {
                    "status": True,
                    "recent_deliveries": [{
                        "tweet_id": "9",
                        "text": "会话A",
                        "delivered_at": "2026-10-30T12:00:00+00:00",
                        "is_retweet": False,
                        "username": "tester",
                        "url": "https://x.com/tester/status/9",
                        "has_media": False,
                    }],
                },
                "session-b": {
                    "status": True,
                    "recent_deliveries": [{
                        "tweet_id": "8",
                        "text": "会话B",
                        "delivered_at": "2026-10-30T13:00:00+00:00",
                        "is_retweet": False,
                    }],
                },
            },
        },
    }}

    async def get(key, default):
        return data.get(key, default)

    async def put(key, value):
        data[key] = copy.deepcopy(value)

    service = module.SubscriptionService(get, put, None, lambda: True)
    plugin = types.SimpleNamespace(
        agent_query_enabled=False,
        subscription_service=service,
    )

    plugin_module = _load_main_module()
    tool = types.MethodType(plugin_module.TwitterPlugin.twitter_recent_pushes, plugin)

    event_a = types.SimpleNamespace(unified_msg_origin="session-a")
    disabled = await tool(event_a)
    assert "未开启" in disabled

    plugin.agent_query_enabled = True
    enabled = await tool(event_a, limit="3")
    assert "会话A" in enabled
    assert "会话B" not in enabled

    empty = await tool(types.SimpleNamespace(unified_msg_origin="session-missing"))
    assert empty == "本会话暂无成功推送摘要。"


@pytest.mark.asyncio
async def test_retweet_query_preserves_source_and_original_author():
    data = {"twitter_subs": {"reposter": {"subscribers": {"group": {}}}}}

    async def get(key, default):
        return data.get(key, default)

    async def put(key, value):
        data[key] = copy.deepcopy(value)

    service = module.SubscriptionService(get, put, None, lambda: True)
    await service.record_delivery("group", "reposter", {
        "tweet_id": "123", "username": "original", "text": "原作者正文",
        "retweet": {"retweeter_username": "reposter"},
    })
    items = await service.list_session_recent_deliveries("group", username="@REPOSTER")
    assert len(items) == 1
    assert items[0]["username"] == "reposter"
    assert items[0]["author_username"] == "original"
    assert items[0]["url"] == "https://x.com/original/status/123"
    output = service.format_recent_deliveries_for_agent(items)
    assert "@reposter 转发了 @original 的帖子" in output
    assert "原作者正文" in output
    assert await service.list_session_recent_deliveries("other") == []


def test_legacy_retweet_does_not_invent_original_author():
    item = module.SubscriptionService._normalize_delivery_record({
        "tweet_id": "123", "username": "reposter", "text": "旧转帖",
        "is_retweet": True, "url": "https://x.com/reposter/status/123",
    }, "reposter")
    assert not item.get("author_username")
    assert item["url"] == "https://x.com/i/status/123"


@pytest.mark.parametrize("media_key", ["images", "videos"])
def test_quote_only_media_is_visible_to_agent(media_key):
    tweet = {"tweet_id": "124", "quote": {media_key: ["https://example.com/media"]}}
    record = module.SubscriptionService.prepare_delivery("group", "tester", tweet)
    assert record.record["has_media"] is True
    assert "含媒体" in module.SubscriptionService.format_recent_deliveries_for_agent([record.record])
