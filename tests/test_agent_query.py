import copy
import importlib.util
import types
from pathlib import Path

import pytest


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

    async def tool(event, username="", limit=5):
        if not plugin.agent_query_enabled:
            return (
                "Twitter 推送摘要查询未开启。"
                "请在插件配置中打开 twitter_agent_query_enabled。"
            )
        try:
            capped = int(limit)
        except (TypeError, ValueError):
            capped = 5
        filter_name = str(username or "").strip().lstrip("@") or None
        items = await plugin.subscription_service.list_session_recent_deliveries(
            event.unified_msg_origin,
            username=filter_name,
            limit=capped,
        )
        return plugin.subscription_service.format_recent_deliveries_for_agent(items)

    event_a = types.SimpleNamespace(unified_msg_origin="session-a")
    disabled = await tool(event_a)
    assert "未开启" in disabled

    plugin.agent_query_enabled = True
    enabled = await tool(event_a, limit="3")
    assert "会话A" in enabled
    assert "会话B" not in enabled

    empty = await tool(types.SimpleNamespace(unified_msg_origin="session-missing"))
    assert empty == "本会话暂无成功推送摘要。"
