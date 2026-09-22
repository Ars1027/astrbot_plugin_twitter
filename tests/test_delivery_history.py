import copy
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "history_subscription_service",
    Path(__file__).resolve().parents[1] / "services/subscription_service.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def store():
    data = {"twitter_subs": {"tester": {
        "since_id": "999", "processed_tweet_ids": ["999"],
        "subscribers": {"group-a": {"status": True}, "group-b": {"status": True}},
    }}}

    async def get(key, default):
        return data.get(key, default)

    async def put(key, value):
        data[key] = copy.deepcopy(value)

    service = module.SubscriptionService(get, put, None, lambda: True)
    return data, get, put, service


@pytest.mark.asyncio
async def test_history_is_bounded_isolated_and_survives_reload(store):
    data, get, put, service = store
    assert await service.get_recent_deliveries("group-a", "tester") == []
    for i in range(7):
        await service.record_delivery("group-a", "TESTER", {
            "tweet_id": str(i), "text": "🌿" * 600,
            "videos": ["large-media-file"], "retweet": {"username": "original"},
        })
    await service.record_delivery("group-a", "tester", {"tweet_id": "4", "text": "again"})
    # A new instance reads the same persisted snapshot, with no network lookup.
    reloaded = module.SubscriptionService(get, put, None, lambda: False)
    records = await reloaded.get_recent_deliveries("group-a", "tester")
    assert [item["tweet_id"] for item in records] == ["4", "6", "5", "3", "2"]
    assert len(records[1]["text"]) == 500
    assert records[1]["truncated"] and records[1]["is_retweet"]
    assert records[1]["delivered_at"].endswith("+00:00")
    assert "large-media-file" not in json.dumps(records)
    assert await reloaded.get_recent_deliveries("group-b", "tester") == []
    assert await reloaded.get_recent_deliveries("group-a", "unknown") is None
    assert data["twitter_subs"]["tester"]["since_id"] == "999"
    assert data["twitter_subs"]["tester"]["processed_tweet_ids"] == ["999"]
    records.clear()
    assert len(await service.get_recent_deliveries("group-a", "tester")) == 5
    await service.add("group-a", "tester", r18=True)
    await service.update("group-a", "tester", {"enabled": False})
    assert len(await service.get_recent_deliveries("group-a", "tester")) == 5


@pytest.mark.asyncio
async def test_unsubscribe_clears_only_its_history_and_late_write_does_not_recreate(store):
    _data, _get, _put, service = store
    for umo in ("group-a", "group-b"):
        await service.record_delivery(umo, "tester", {"tweet_id": "1"})
    await service.remove("group-a", "tester")
    await service.record_delivery("group-a", "tester", {"tweet_id": "2"})
    assert await service.get_recent_deliveries("group-a", "tester") is None
    assert len(await service.get_recent_deliveries("group-b", "tester")) == 1
    await service.add("group-a", "tester")
    assert await service.get_recent_deliveries("group-a", "tester") == []
    await service.clear()
    assert await service.get_recent_deliveries("group-b", "tester") is None


@pytest.mark.asyncio
async def test_history_failure_does_not_mutate_live_subscription_snapshot(store):
    data, get, _put, _service = store
    before = copy.deepcopy(data)

    async def fail(_key, _value):
        raise OSError("KV unavailable")

    service = module.SubscriptionService(get, fail, None, lambda: True)
    with pytest.raises(OSError):
        await service.record_delivery("group-a", "tester", {"tweet_id": "1"})
    assert data == before


@pytest.mark.asyncio
async def test_history_ignores_invalid_ids_and_keeps_quote_summary(store):
    _data, _get, _put, service = store
    for tweet_id in ("", "<invalid>", "１２３", "1" * 41):
        await service.record_delivery("group-a", "tester", {"tweet_id": tweet_id})
    assert await service.get_recent_deliveries("group-a", "tester") == []
    await service.record_delivery("group-a", "tester", {
        "tweet_id": "123", "text": "原文", "quote": {"text": "引用正文"}
    })
    record = (await service.get_recent_deliveries("group-a", "tester"))[0]
    assert record["text"] == "原文\n\n引用：引用正文"
    assert record["truncated"] is False
