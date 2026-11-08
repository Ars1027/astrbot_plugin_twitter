import asyncio
import copy

import pytest

from support.fakes import Plain, TG_GROUP, TG_TOPIC


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["qq-bot:GroupMessage:100", TG_GROUP, TG_TOPIC])
async def test_group_clear_is_atomic_and_preserves_other_sessions(plugin_env, target, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    retained = {umo: {"status": False, "pending_delivery_ids": ["102"],
                      "pending_skip_ids": ["103"], "recent_deliveries": [{"tweet_id": "99"}]}
                for umo in ("other-bot:GroupMessage:100", "qq-bot:FriendMessage:100",
                            "my-bot:GroupMessage:-100123#43", "my-bot:FriendMessage:100")}
    data["twitter_subs"] = {
        "shared": {"since_id": "101", "pending_tweet_items": [{"tweet_id": "102", "is_retweet": True}],
                   "processed_tweet_ids": ["101"],
                   "subscribers": {target: {"status": True}, **copy.deepcopy(retained)}},
        "exclusive": {"since_id": "50", "subscribers": {target: {"status": False}}},
        "unrelated": {"since_id": "80", "subscribers": {"qq-bot:GroupMessage:200": {}}},
    }
    data["twitter_telegram_groups"] = {TG_GROUP: {"group_name": "保留群组"}}
    data["twitter_retweet_dedup_seen"] = {target: ["98"], "other-bot:GroupMessage:100": ["97"]}
    before = copy.deepcopy(data)
    writes = []

    async def put(key, value):
        writes.append(key)
        data[key] = copy.deepcopy(value)

    plugin.put_kv_data = put
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = {"umo": target}
    payload, status = await controller.clear_group_subscriptions()
    assert status == 200 and payload == {"saved": True, "removed": 2}
    assert writes == ["twitter_subs"]
    expected = copy.deepcopy(before)
    expected["twitter_subs"].pop("exclusive")
    expected["twitter_subs"]["shared"]["subscribers"].pop(target)
    assert data == expected
    # 重复请求或旧页面上的空群是幂等操作，无需额外写入。
    payload, status = await controller.clear_group_subscriptions()
    assert status == 200 and payload["removed"] == 0
    assert writes == ["twitter_subs"] and data == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"umo": None}, {"umo": "invalid"},
                                     {"umo": "qq-bot:FriendMessage:100"},
                                     {"umo": "qq-bot:OtherMessage:100"}])
async def test_group_clear_rejects_invalid_or_non_group_targets(plugin_env, payload, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"subscribers": {"qq-bot:GroupMessage:100": {}}}}
    before = copy.deepcopy(data)
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = payload
    result, status = await controller.clear_group_subscriptions()
    assert status == 400 and not result.get("saved")
    assert data == before


@pytest.mark.asyncio
async def test_group_clear_storage_failure_preserves_live_state_and_returns_error(plugin_env, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {
        "qq-bot:GroupMessage:100": {"pending_delivery_ids": ["101"]},
    }}}
    before = copy.deepcopy(data)

    async def get_live(key, default):
        return data.get(key, default)

    async def failed_put(_key, _value):
        raise OSError("secret database details")

    plugin.get_kv_data = get_live
    plugin.put_kv_data = failed_put
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = {"umo": "qq-bot:GroupMessage:100"}
    result, status = await controller.clear_group_subscriptions()
    assert status == 500 and not result.get("saved")
    assert "secret" not in str(result)
    assert data == before


@pytest.mark.asyncio
async def test_clear_group_keeps_queued_delivery_for_other_group_only(plugin_env):
    _module, make, data, calls, _fail = plugin_env
    plugin = make(collective=True)
    other = "qq-bot:GroupMessage:200"
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_GROUP: {}, other: {}}}}

    async def build(*_args, **_kwargs):
        return [Plain("body")]

    plugin.message_service.build_message_chain = build
    await plugin.delivery_service.push_to_subscribers("tester", {"tweet_id": "101", "username": "tester"})
    assert calls == []
    # 已缓存但尚未发送的集体推送也应遵循新的订阅状态。
    await plugin.subscription_service.clear_session(TG_GROUP)
    result = await plugin.delivery_service.flush_collected()
    assert result.successful_authors == {"tester"}
    assert [umo for umo, _chain in calls] == [other]


@pytest.mark.asyncio
async def test_clear_group_and_other_group_update_do_not_overwrite_each_other(plugin_env):
    _module, make, data, _calls, _fail = plugin_env
    service = make().subscription_service
    target = "qq-bot:GroupMessage:100"
    other = "qq-bot:GroupMessage:200"
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {target: {}, other: {}}}}
    cleared, updated = await asyncio.gather(service.clear_session(target), service.update(other, "tester", {"enabled": False}))
    assert cleared["relations"] == 1 and updated["ok"]
    assert data["twitter_subs"]["tester"] == {"since_id": "100", "subscribers": {other: {"status": False}}}
