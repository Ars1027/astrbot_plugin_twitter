import asyncio
import copy
import sys
import types

import pytest
from test_provider_initialization import _load_main_module
from test_twitter_webui import FakeContext, FakePlatform, _load_webui_module

GROUP = "bot-a:GroupMessage:100"
MEMBER_A = "bot-a:GroupMessage:111_100"
MEMBER_B = "bot-a:GroupMessage:222_100"


def make_event(umo=MEMBER_A, *, platform="aiocqhttp", group="100", message=""):
    return types.SimpleNamespace(
        unified_msg_origin=umo,
        get_platform_name=lambda: platform,
        get_group_id=lambda: group,
        get_self_id=lambda: "999",
        message_str=message,
        plain_result=lambda text: text,
        chain_result=lambda chain: chain,
    )


@pytest.fixture
def session_store():
    previous_web_api = sys.modules.get("astrbot.api.web")
    module = _load_main_module()
    data = {}
    writes = []

    async def get(key, default):
        return copy.deepcopy(data.get(key, default))

    async def put(key, value):
        writes.append(key)
        data[key] = copy.deepcopy(value)

    service = module.SubscriptionService(get, put, None, lambda: True)
    plugin = module.TwitterPlugin.__new__(module.TwitterPlugin)
    plugin.subscription_service = service
    plugin._provider_ready = True
    plugin.agent_query_enabled = True
    yield module, plugin, data, writes
    if previous_web_api is None:
        sys.modules.pop("astrbot.api.web", None)
    else:
        sys.modules["astrbot.api.web"] = previous_web_api


@pytest.mark.parametrize("umo, platform, group, expected", [
    (MEMBER_A, "aiocqhttp", "100", GROUP),
    (MEMBER_B, "aiocqhttp", "100", GROUP),
    (GROUP, "aiocqhttp", "100", GROUP),
    ("bot-b:GroupMessage:111_100", "aiocqhttp", "100", "bot-b:GroupMessage:100"),
    ("bot-a:FriendMessage:111", "aiocqhttp", "", "bot-a:FriendMessage:111"),
    (MEMBER_A, "aiocqhttp", "", MEMBER_A),
    (MEMBER_A, "matrix", "100", MEMBER_A),
])
def test_subscription_target_uses_real_qq_group_without_changing_event(
    session_store, umo, platform, group, expected,
):
    module, _plugin, _data, _writes = session_store
    event = make_event(umo, platform=platform, group=group)
    assert module.SubscriptionService.session_target(event) == expected
    assert event.unified_msg_origin == umo


@pytest.mark.asyncio
async def test_members_manage_one_subscription_and_webui_shows_one_real_group(session_store):
    _module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {"screen_name": "Tester", "subscribers": {}}}
    event_a = make_event(message="/推特关注 tester")
    event_b = make_event(MEMBER_B, message="/推特关注 tester")

    for event in (event_a, event_b):
        results = [item async for item in plugin.follow_twitter(event, "tester")]
        assert results
    assert list(data["twitter_subs"]["tester"]["subscribers"]) == [GROUP]

    results = [item async for item in plugin.list_follows(event_b)]
    assert "@tester" in results[0][0].nodes[0].content[0].text
    await plugin.subscription_service.record_delivery(GROUP, "tester", {
        "tweet_id": "101", "username": "tester", "text": "群推送摘要",
    })
    assert "群推送摘要" in await plugin.twitter_recent_pushes(event_b)
    assert await plugin.twitter_recent_pushes(make_event(group="200")) == "本会话暂无成功推送摘要。"

    results = [item async for item in plugin.toggle_push(event_b, "关闭")]
    assert "影响 1 个订阅" in results[0]
    assert data["twitter_subs"]["tester"]["subscribers"][GROUP]["status"] is False

    webui = _load_webui_module()
    context = FakeContext([FakePlatform("bot-a", [{"group_id": "100", "group_name": "测试群"}])])
    controller = webui.TwitterWebUIController(plugin, context)
    overview = await controller._build_overview()
    assert len(overview["groups"]) == 1
    group = overview["groups"][0]
    assert (group["umo"], group["group_id"], group["available"]) == (GROUP, "100", True)
    assert len(group["subscriptions"]) == 1
    assert overview["totals"]["subscriptions"] == 1

    results = [item async for item in plugin.unfollow_twitter(event_b, "tester")]
    assert results
    assert data["twitter_subs"] == {}


@pytest.mark.asyncio
async def test_batch_commands_share_group_target(session_store):
    _module, plugin, data, _writes = session_store
    data["twitter_subs"] = {
        name: {"subscribers": {}} for name in ("alice", "bob")
    }
    event_a = make_event(message="/推特批量关注 alice bob")
    results = [item async for item in plugin.batch_follow_twitter(event_a)]
    assert results
    assert all(list(author["subscribers"]) == [GROUP] for author in data["twitter_subs"].values())
    event_b = make_event(MEMBER_B, message="/推特批量取关 alice bob")
    results = [item async for item in plugin.batch_unfollow_twitter(event_b)]
    assert results
    assert data["twitter_subs"] == {}


@pytest.mark.asyncio
async def test_migration_preserves_group_options_receipts_history_and_dedup(session_store):
    _module, plugin, data, writes = session_store

    def history(tweet, time):
        return {"tweet_id": tweet, "delivered_at": f"2026-11-05T{time}:00+00:00"}
    data["twitter_subs"] = {"tester": {
        "since_id": "10", "processed_tweet_ids": ["10"],
        "pending_tweet_items": [{"tweet_id": "11", "username": "tester"}],
        "subscribers": {
            MEMBER_B: {"status": True, "r18": True, "media": True,
                       "pending_delivery_ids": ["11"], "pending_skip_ids": ["13"],
                       "recent_deliveries": [history("11", "14:00")]},
            GROUP: {"status": False, "r18": False, "media": False,
                    "pending_delivery_ids": ["12"], "recent_deliveries": [history("11", "12:00")]},
            MEMBER_A: {"status": True, "pending_skip_ids": ["14"],
                       "recent_deliveries": [history("15", "13:00")]},
            "other:GroupMessage:111_100": {"status": True},
            "bot-a:FriendMessage:111_100": {"status": True},
            "bot-a:GroupMessage:abc_def": {"status": True},
        },
    }}
    data["twitter_retweet_dedup_seen"] = {
        MEMBER_A: ["2", "3"], MEMBER_B: ["3", "4"], GROUP: ["1"],
    }
    before = copy.deepcopy(data["twitter_subs"]["tester"])
    assert await plugin.subscription_service.migrate_qq_group_sessions({"bot-a"}) == 2
    author = data["twitter_subs"]["tester"]
    assert {key: value for key, value in author.items() if key != "subscribers"} == {
        key: value for key, value in before.items() if key != "subscribers"
    }
    group = author["subscribers"][GROUP]
    assert (group["status"], group["r18"], group["media"]) == (False, False, False)
    assert set(group["pending_delivery_ids"]) == {"11", "12"}
    assert set(group["pending_skip_ids"]) == {"13", "14"}
    assert group["recent_deliveries"] == [history("11", "14:00"), history("15", "13:00")]
    assert set(data["twitter_retweet_dedup_seen"][GROUP]) == {"1", "2", "3", "4"}
    for umo in ("other:GroupMessage:111_100", "bot-a:FriendMessage:111_100", "bot-a:GroupMessage:abc_def"):
        assert author["subscribers"][umo] == before["subscribers"][umo]
    assert MEMBER_A not in author["subscribers"] and MEMBER_B not in author["subscribers"]
    assert writes == ["twitter_retweet_dedup_seen", "twitter_subs"]
    snapshot = copy.deepcopy(data)
    assert await plugin.subscription_service.migrate_qq_group_sessions({"bot-a"}) == 0
    assert data == snapshot
    assert writes == ["twitter_retweet_dedup_seen", "twitter_subs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_key", ["twitter_retweet_dedup_seen", "twitter_subs"])
async def test_migration_write_failure_keeps_old_subscriptions_and_can_retry(session_store, failed_key):
    _module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {"subscribers": {MEMBER_A: {"status": True}}}}
    data["twitter_retweet_dedup_seen"] = {MEMBER_A: ["11"]}
    original_subs = copy.deepcopy(data["twitter_subs"])
    service = plugin.subscription_service
    put = service._put_kv_data

    async def fail(key, value):
        if key == failed_key:
            raise OSError("storage unavailable")
        await put(key, value)

    service._put_kv_data = fail
    with pytest.raises(OSError, match="storage unavailable"):
        await service.migrate_qq_group_sessions({"bot-a"})
    assert data["twitter_subs"] == original_subs
    assert data["twitter_retweet_dedup_seen"][MEMBER_A] == ["11"]
    service._put_kv_data = put
    assert await service.migrate_qq_group_sessions({"bot-a"}) == 1
    assert list(data["twitter_subs"]["tester"]["subscribers"]) == [GROUP]
    assert data["twitter_retweet_dedup_seen"][GROUP] == ["11"]


@pytest.mark.asyncio
async def test_migration_discovers_only_qq_platforms_and_chooses_stable_options(session_store):
    _module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {"subscribers": {
        MEMBER_B: {"status": True}, MEMBER_A: {"status": False},
        "matrix:GroupMessage:111_100": {"status": True},
        "bot-b:GroupMessage:111_100": {"status": True},
    }}}
    plugin.context = FakeContext([
        FakePlatform("bot-a"), FakePlatform("bot-b"), FakePlatform("matrix", name="matrix"),
    ])
    await plugin._migrate_group_subscriptions()
    subscribers = data["twitter_subs"]["tester"]["subscribers"]
    assert subscribers[GROUP]["status"] is False
    assert "bot-b:GroupMessage:100" in subscribers
    assert "matrix:GroupMessage:111_100" in subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("startup", ["hot_reload", "cold_start", "poll_retry"])
async def test_startup_migrates_before_polling_without_resending_acknowledged_tweets(
    session_store, transport, startup,
):
    module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {
        "since_id": "100",
        "pending_tweet_items": [{"tweet_id": "101", "username": "tester"}],
        "subscribers": {
            MEMBER_A: {"status": True, "pending_delivery_ids": ["101"]},
            MEMBER_B: {"status": True},
        },
    }}
    sent = []
    timeline_calls = []

    async def send_message(umo, _chain):
        sent.append(umo)
        return True

    async def timeline(_username, since_id):
        timeline_calls.append(since_id)
        return [{"tweet_id": tweet_id, "username": "tester"}
                for tweet_id in ("101", "102") if int(tweet_id) > int(since_id)]

    async def get_tweet(username, tweet_id):
        return {"status": True, "username": username, "tweet_id": tweet_id, "text": "body"}

    platform = FakePlatform("bot-a", [{"group_id": "100"}])
    context = FakeContext([platform] if startup == "hot_reload" else [])
    context.send_message = send_message
    initialized = module.TwitterPlugin(context, {
        "twitter_data_provider": "fxtwitter",
        "twitter_use_node": transport != "plain",
        "twitter_collective_forward": transport == "collective",
    })
    initialized.get_kv_data = plugin.subscription_service._get_kv_data
    initialized.put_kv_data = plugin.subscription_service._put_kv_data
    initialized.twitter_api.get_user_timeline_items = timeline
    initialized.twitter_api.get_tweet = get_tweet

    waits = 0

    async def next_poll():
        nonlocal waits
        waits += 1
        if startup != "hot_reload" and waits == 1:
            # initialize() 和首轮后台任务发生在平台实例创建之前。
            assert sent == []
            assert timeline_calls == []
            assert list(data["twitter_subs"]["tester"]["subscribers"]) == [MEMBER_A, MEMBER_B]
            context.platform_manager.platform_insts.append(platform)
            if startup == "cold_start":
                await initialized.on_astrbot_loaded()
                assert list(data["twitter_subs"]["tester"]["subscribers"]) == [GROUP]
        else:
            initialized._running = False

    initialized._wait_for_next_poll = next_poll
    try:
        await initialized.initialize()
        await asyncio.wait_for(initialized._poll_task, timeout=10)
        assert list(data["twitter_subs"]["tester"]["subscribers"]) == [GROUP]
        assert sent == []
        assert data["twitter_subs"]["tester"]["since_id"] == "101"
        await initialized._check_all_subscriptions()
        assert sent == [GROUP]
        assert data["twitter_subs"]["tester"]["since_id"] == "102"
        await initialized._check_all_subscriptions()
        assert sent == [GROUP]
    finally:
        await initialized.terminate()


@pytest.mark.asyncio
async def test_platform_ready_migrates_even_without_polling_and_commands_manage_old_subscriptions(
    session_store, monkeypatch,
):
    module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {"subscribers": {MEMBER_A: {"status": True}}}}
    context = FakeContext()
    initialized = module.TwitterPlugin(context, {})
    initialized.get_kv_data = plugin.subscription_service._get_kv_data
    initialized.put_kv_data = plugin.subscription_service._put_kv_data

    async def unavailable(_websites):
        return None

    monkeypatch.setattr(initialized.twitter_api, "check_website_available", unavailable)
    try:
        await initialized.initialize()
        assert initialized._poll_task is None
        context.platform_manager.platform_insts.append(FakePlatform("bot-a"))
        await initialized.on_astrbot_loaded()

        event = make_event(MEMBER_B)
        results = [item async for item in initialized.list_follows(event)]
        assert "@tester" in results[0][0].nodes[0].content[0].text
        results = [item async for item in initialized.toggle_push(event, "关闭")]
        assert "影响 1 个订阅" in results[0]
        assert data["twitter_subs"]["tester"]["subscribers"] == {GROUP: {"status": False}}
        results = [item async for item in initialized.unfollow_twitter(event, "tester")]
        assert results
        assert data["twitter_subs"] == {}
    finally:
        await initialized.terminate()


@pytest.mark.asyncio
async def test_migration_failure_blocks_polling_and_retries_before_next_snapshot(session_store):
    _module, plugin, data, _writes = session_store
    data["twitter_subs"] = {"tester": {"subscribers": {MEMBER_A: {"status": True}}}}
    plugin.context = FakeContext([FakePlatform("bot-a")])
    snapshots = []

    async def check_all():
        snapshots.append(copy.deepcopy(data["twitter_subs"]))

    plugin.polling_service = types.SimpleNamespace(check_all=check_all)
    put = plugin.subscription_service._put_kv_data

    async def fail(_key, _value):
        raise OSError("storage unavailable")

    plugin.subscription_service._put_kv_data = fail
    with pytest.raises(OSError, match="storage unavailable"):
        await plugin._check_all_subscriptions()
    assert snapshots == []
    assert list(data["twitter_subs"]["tester"]["subscribers"]) == [MEMBER_A]

    plugin.subscription_service._put_kv_data = put
    await plugin._check_all_subscriptions()
    assert list(snapshots[0]["tester"]["subscribers"]) == [GROUP]
