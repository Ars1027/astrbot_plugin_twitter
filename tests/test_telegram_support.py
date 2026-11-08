import asyncio
import copy
import sys
import types

import pytest

from support.fakes import Image, Nodes, Plain, Video, FakeContext, FakePlugin
from support.fakes import TG_GROUP, TG_TOPIC, TG_PRIVATE, telegram_platform, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("umo", [TG_GROUP, TG_TOPIC, TG_PRIVATE])
@pytest.mark.parametrize("collective", [False, True])
async def test_telegram_and_qq_keep_platform_specific_delivery(plugin_env, umo, collective):
    _module, make, data, calls, _fail = plugin_env
    qq = "qq-bot:GroupMessage:456"
    data["twitter_subs"] = {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True}, qq: {"status": True}},
    }}
    plugin = make(collective=collective)

    async def build(*_args, **_kwargs):
        return [Plain("正文"), Image(file="https://example.com/image.jpg"),
                Video(file="https://example.com/video.mp4")]

    plugin.message_service.build_message_chain = build
    result = await plugin.delivery_service.push_to_subscribers("tester", {
        "tweet_id": "101", "username": "tester", "text": "正文",
    })
    if collective:
        assert result.state.value == "queued"
        assert not calls
        result = await plugin.delivery_service.flush_collected()
        assert not result.failed_authors
    tg = [chain for target, chain in calls if target == umo]
    assert [type(part) for request in tg for part in request] == [Plain, Image, Video]
    assert all(len(request) == 1 for request in tg)
    qq_chains = [chain for target, chain in calls if target == qq]
    assert isinstance(qq_chains[0][0], Nodes)
    assert {receipt.umo for receipt in result.delivered_tweets} == {umo, qq}


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_telegram_failure_keeps_cursor_and_retries_only_failed_session(plugin_env, collective):
    _module, make, data, calls, fail = plugin_env
    data["twitter_subs"] = {"tester": {
        "since_id": "100", "subscribers": {
            TG_TOPIC: {"status": True}, TG_PRIVATE: {"status": True},
        },
    }}
    fail.add(TG_TOPIC)
    plugin = make(collective=collective)

    async def timeline(_username, since_id):
        return [{"tweet_id": "101", "username": "tester"}] if int(since_id) < 101 else []

    async def tweet(username, tweet_id):
        return {"status": True, "tweet_id": tweet_id, "username": username, "text": "正文"}

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = tweet
    await plugin.polling_service.check_all()
    author = data["twitter_subs"]["tester"]
    assert author["since_id"] == "100"
    assert author["subscribers"][TG_PRIVATE]["pending_delivery_ids"] == ["101"]
    assert not author["subscribers"][TG_TOPIC].get("pending_delivery_ids")
    calls.clear()
    fail.clear()
    await plugin.polling_service.check_all()
    assert data["twitter_subs"]["tester"]["since_id"] == "101"
    assert [target for target, _chain in calls] == [TG_TOPIC]


@pytest.mark.asyncio
async def test_telegram_commands_keep_topics_and_private_chats_separate(plugin_env):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"subscribers": {}}}
    topic = make_event(TG_TOPIC, "/twitter_follow tester")
    private = make_event(TG_PRIVATE, "/twitter_follow tester")
    for event in (topic, private):
        assert [item async for item in plugin.follow_twitter(event, "tester")]
    assert set(data["twitter_subs"]["tester"]["subscribers"]) == {TG_TOPIC, TG_PRIVATE}
    result = [item async for item in plugin.list_follows(topic)]
    assert isinstance(result[0], str) and "@tester" in result[0]
    assert [item async for item in plugin.unfollow_twitter(topic, "tester")]
    assert list(data["twitter_subs"]["tester"]["subscribers"]) == [TG_PRIVATE]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["test", "parse"])
async def test_telegram_direct_commands_use_plain_text_images_and_videos(plugin_env, command):
    _module, make, _data, calls, _fail = plugin_env
    plugin = make()

    async def timeline(_username):
        return [{"tweet_id": "101", "username": "tester"}]

    async def tweet(_username, _tweet_id):
        return {"status": True, "username": "tester", "tweet_id": "101", "text": "正文"}

    async def build(*_args, **_kwargs):
        return [Plain("正文"), Image(file="https://example.com/a.jpg"), Video(file="https://example.com/a.mp4")]

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = tweet
    plugin.message_service.build_message_chain = build
    event = make_event(TG_TOPIC, "https://x.com/tester/status/101")
    if command == "test":
        results = [item async for item in plugin.test_tweet(event, "tester")]
    else:
        match = _module.TWITTER_LINK_PATTERN.search(event.message_str)
        results = [item async for item in plugin._handle_tweet_link(event, match, report_errors=True)]
    assert not any(isinstance(item, list) for item in results)
    assert all(target == TG_TOPIC for target, _request in calls)
    assert [type(part) for _target, request in calls for part in request] == [Plain, Image, Video]
    assert all(len(request) == 1 for _target, request in calls)


@pytest.mark.asyncio
async def test_webui_discovers_existing_telegram_groups_and_topics_and_adds_author(plugin_env, webui_module):
    web = webui_module
    plugin = FakePlugin({"tester": {"subscribers": {TG_TOPIC: {}, TG_PRIVATE: {}}}})
    platform = telegram_platform()
    controller = web.TwitterWebUIController(plugin, FakeContext([platform]))
    overview = await controller._build_overview()
    assert len(overview["groups"]) == 1
    group = overview["groups"][0]
    assert group["umo"] == TG_TOPIC and group["available"] is True
    assert group["group_name"] == "Telegram 测试群 · 话题 42"
    assert overview["other_sessions"][0]["umo"] == TG_PRIVATE
    assert ("chat", -100123) in platform.client.calls
    web.fake_request.payload = {"umo": TG_TOPIC, "username": "newuser"}
    _payload, status = await controller.add_subscription()
    assert status == 200
    assert plugin.last_add[:2] == (TG_TOPIC, "newuser")


@pytest.mark.asyncio
async def test_webui_verifies_each_telegram_chat_once_for_multiple_topics(plugin_env, webui_module):
    web = webui_module
    second_topic = "my-bot:GroupMessage:-100123#43"
    plugin = FakePlugin({"tester": {"subscribers": {TG_TOPIC: {}, second_topic: {}}}})
    platform = telegram_platform()
    controller = web.TwitterWebUIController(plugin, FakeContext([platform]))
    overview = await controller._build_overview()
    assert sorted(group["group_id"] for group in overview["groups"]) == ["-100123#42", "-100123#43"]
    # 同一 chat 的多个话题只校验一次群与机器人身份。
    assert [call for call in platform.client.calls if call[0] == "chat"] == [("chat", -100123)]
    assert [call for call in platform.client.calls if call[0] == "member"] == [("member", -100123, 9876)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    ("left", None), ("kicked", None), ("restricted", None),
    ("member", RuntimeError("URL contains secret token")),
])
async def test_webui_unavailable_telegram_keeps_subs_and_prevents_new_ones(plugin_env, status, error, webui_module):
    web = webui_module
    plugin = FakePlugin({"tester": {"subscribers": {TG_GROUP: {}}}})
    controller = web.TwitterWebUIController(plugin, FakeContext([telegram_platform(status=status, error=error)]))
    overview = await controller._build_overview()
    assert overview["groups"][0]["available"] is False
    assert len(overview["groups"][0]["subscriptions"]) == 1
    assert "secret" not in str(overview)
    web.fake_request.payload = {"umo": TG_GROUP, "username": "newuser"}
    _payload, code = await controller.add_subscription()
    assert code in {400, 503}
    assert plugin.last_add is None
    assert "secret" not in str(_payload)


@pytest.mark.asyncio
async def test_webui_cannot_create_unverified_topic_and_handles_empty_bot(plugin_env, webui_module):
    web = webui_module
    plugin = FakePlugin()
    controller = web.TwitterWebUIController(plugin, FakeContext([telegram_platform()]))
    overview = await controller._build_overview()
    assert overview["groups"] == []
    assert overview["group_sources"][0]["available"] is True
    web.fake_request.payload = {"umo": TG_TOPIC, "username": "tester"}
    _payload, status = await controller.add_subscription()
    assert status == 400 and plugin.last_add is None


@pytest.mark.asyncio
async def test_webui_restricted_telegram_bot_requires_sending_permission(plugin_env, webui_module):
    web = webui_module
    plugin = FakePlugin({"tester": {"subscribers": {TG_GROUP: {}}}})
    platform = telegram_platform(status="restricted", is_member=True)
    controller = web.TwitterWebUIController(plugin, FakeContext([platform]))
    web.fake_request.payload = {"umo": TG_GROUP, "username": "tester"}
    payload, status = await controller.add_subscription()
    assert status == 400 and "发言权限" in payload["message"]
    assert plugin.last_add is None
    platform.client.can_send_messages = True
    _payload, status = await controller.add_subscription()
    assert status == 200


@pytest.mark.asyncio
async def test_telegram_video_failure_sends_link_and_preserves_primary_receipt(plugin_env):
    _module, make, data, calls, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"subscribers": {TG_TOPIC: {}}}}
    send = plugin.context.send_message

    async def video_fails(umo, chain):
        if isinstance(chain.chain[0], Video):
            raise OSError("video too large")
        return await send(umo, chain)

    async def build(*_args, **_kwargs):
        return [Plain("正文"), Video(file="https://example.com/a.mp4")]

    plugin.context.send_message = video_fails
    plugin.message_service.build_message_chain = build
    result = await plugin.delivery_service.push_to_subscribers("tester", {
        "tweet_id": "101", "username": "tester", "text": "正文",
    })
    assert result.state.value == "delivered"
    assert len(result.delivered_tweets) == 1
    assert "https://example.com/a.mp4" in calls[-1][1][0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_telegram_failed_screenshot_page_is_not_acknowledged(plugin_env, collective):
    module, make, data, _calls, _fail = plugin_env
    plugin = make(collective=collective)
    data["twitter_subs"] = {"tester": {"subscribers": {TG_GROUP: {}}}}
    message_module = sys.modules[f"{module.__package__}.services.tweet_message_service"]
    pages = [Image(file=f"https://example.com/page-{index}.png") for index in (1, 2)]

    async def build(*_args, **_kwargs):
        return message_module.TweetMessageChain([
            *pages, Plain("https://x.com/tester/status/101"),
        ], required_images=pages)

    async def send(_umo, chain):
        assert not any(isinstance(part, Nodes) for part in chain.chain)
        return not any(part is pages[1] for part in chain.chain)

    plugin.message_service.build_message_chain = build
    plugin.context.send_message = send
    result = await plugin.delivery_service.push_to_subscribers("tester", {
        "tweet_id": "101", "username": "tester", "text": "正文",
    })
    if collective:
        result = await plugin.delivery_service.flush_collected()
        assert result.failed_authors == {"tester"}
    else:
        assert result.state.value == "failed"
    assert not result.delivered_tweets
    assert not result.recent_deliveries


@pytest.mark.asyncio
async def test_manual_telegram_group_persists_without_subscriptions_and_is_idempotent(plugin_env, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = {"platform_id": "my-bot", "group_id": " -100123 "}
    result, status = await controller.add_telegram_group()
    assert status == 200 and result["saved"] is True and result["created"] is True
    assert result["umo"] == TG_GROUP
    assert "twitter_subs" not in data
    assert list(data["twitter_telegram_groups"]) == [TG_GROUP]
    result, status = await controller.add_telegram_group()
    assert status == 200 and result["created"] is False
    reloaded = make()
    controller = web.TwitterWebUIController(reloaded, reloaded.context)
    overview = await controller._build_overview()
    assert len(overview["groups"]) == 1
    group = overview["groups"][0]
    assert group["umo"] == TG_GROUP and group["available"] is True
    assert group["group_name"] == "Telegram 测试群" and group["subscriptions"] == []
    web.fake_request.payload = {"umo": TG_GROUP, "username": "tester"}
    data["twitter_subs"] = {"tester": {"subscribers": {}}}
    _result, status = await controller.add_subscription()
    assert status == 200
    assert TG_GROUP in data["twitter_subs"]["tester"]["subscribers"]
    await reloaded.subscription_service.remove(TG_GROUP, "tester")
    overview = await controller._build_overview()
    assert len(overview["groups"]) == 1 and overview["groups"][0]["subscriptions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    [], {"platform_id": "qq-bot", "group_id": "-100123"},
    {"platform_id": "missing", "group_id": "-100123"},
    {"platform_id": "my-bot", "group_id": 100123},
    {"platform_id": "my-bot", "group_id": "12345"},
    {"platform_id": "my-bot", "group_id": "-100123#42"},
    {"platform_id": "my-bot", "group_id": "https://t.me/example"},
    {"platform_id": "my-bot", "group_id": "-0"},
    {"platform_id": "my-bot", "group_id": "-" + "1" * 40},
])
async def test_manual_telegram_group_rejects_invalid_target_without_saving(plugin_env, payload, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = payload
    result, status = await controller.add_telegram_group()
    assert status == 400 and not result.get("saved")
    assert data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["left", "kicked", "channel", "timeout", "network", "save"])
async def test_manual_telegram_group_failure_does_not_create_group(plugin_env, failure, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    client = plugin.context.platform_manager.platform_insts[0].client
    web.fake_request.payload = {"platform_id": "my-bot", "group_id": "-100123"}
    if failure in {"left", "kicked"}:
        client.status = failure
    elif failure == "channel":
        async def get_channel(**_kwargs):
            return types.SimpleNamespace(type="channel", title="不是群组")
        client.get_chat = get_channel
    elif failure in {"timeout", "network"}:
        client.error = TimeoutError() if failure == "timeout" else RuntimeError("secret token")
    else:
        async def put_fails(_key, _value):
            raise OSError("storage unavailable")
        plugin.put_kv_data = put_fails
    result, status = await controller.add_telegram_group()
    assert status == (500 if failure == "save" else 503 if failure in {"timeout", "network"} else 400)
    assert not result.get("saved") and "secret" not in str(result)
    assert data == {}


@pytest.mark.asyncio
async def test_registered_empty_group_stays_visible_when_bot_leaves(plugin_env, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = {"platform_id": "my-bot", "group_id": "-100123"}
    _result, status = await controller.add_telegram_group()
    assert status == 200
    client = plugin.context.platform_manager.platform_insts[0].client
    client.status = "left"
    overview = await controller._build_overview()
    group = overview["groups"][0]
    assert group["umo"] == TG_GROUP and group["available"] is False
    assert group["group_name"] == "Telegram 测试群" and group["subscriptions"] == []
    web.fake_request.payload = {"umo": TG_GROUP, "username": "tester"}
    _result, status = await controller.add_subscription()
    assert status == 400 and "twitter_subs" not in data
    plugin.context.platform_manager.platform_insts = []
    overview = await controller._build_overview()
    assert len(overview["groups"]) == 1
    assert overview["groups"][0]["umo"] == TG_GROUP
    assert overview["groups"][0]["available"] is False


@pytest.mark.asyncio
async def test_registered_groups_concurrent_writes_preserve_platform_identity(plugin_env):
    _module, make, data, _calls, _fail = plugin_env
    service = make().subscription_service
    group = {"group_id": "-100123", "group_name": "测试群"}
    assert await asyncio.gather(
        service.register_telegram_group("my-bot", group),
        service.register_telegram_group("second-bot", group),
    ) == [True, True]
    assert set(data["twitter_telegram_groups"]) == {TG_GROUP, "second-bot:GroupMessage:-100123"}
    snapshot = await service.get_telegram_groups()
    snapshot[TG_GROUP]["group_name"] = "不应修改 KV"
    assert data["twitter_telegram_groups"][TG_GROUP]["group_name"] == "测试群"


@pytest.mark.asyncio
async def test_screenshot_link_does_not_send_empty_text_or_repeat_screenshot(plugin_env):
    _module, make, _data, _calls, _fail = plugin_env
    plugin = make()
    delivered = []
    rejected = []

    async def render(*_args, **_kwargs):
        return "https://render.test/card.png"

    async def telegram_send(_umo, message):
        # 模拟真实 TG 适配器逐组件发送，空白文本被 Bot API 拒绝。
        for part in message.chain:
            if isinstance(part, Plain) and not part.text.strip():
                rejected.append(part.text)
                raise ValueError("Message text is empty")
            delivered.append(part)
        return True

    plugin.message_service.html_render = render
    plugin.context.send_message = telegram_send
    chain = await plugin.message_service.build_screenshot_tweet_chain(
        "tester", {"tweet_id": "101", "username": "tester", "text": "正文"},
    )
    # 直接使用相册功能加入前的普通消息路径。
    assert await plugin.delivery_service.send_plain_chain_resilient(TG_GROUP, chain)
    assert len([part for part in delivered if isinstance(part, Image)]) == 1
    assert rejected == []
    assert [part.text for part in delivered if isinstance(part, Plain)] == ["https://x.com/tester/status/101"]


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", ["online", "offline", "platform_removed"])
async def test_remove_manual_group_after_unsubscribe_preserves_other_groups_and_state(plugin_env, availability, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    service = plugin.subscription_service
    other = "second-bot:GroupMessage:-100123"
    await service.register_telegram_group("my-bot", {"group_id": "-100123", "group_name": "待删除群"})
    await service.register_telegram_group("second-bot", {"group_id": "-100123", "group_name": "保留群"})
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {
        TG_GROUP: {"status": False, "pending_delivery_ids": ["101"]},
        other: {"status": True, "pending_delivery_ids": ["102"]},
    }}}
    data["twitter_retweet_dedup_seen"] = {TG_GROUP: ["99"], other: ["98"]}
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    if availability == "offline":
        plugin.context.platform_manager.platform_insts[0].client.error = RuntimeError("secret token")
    elif availability == "platform_removed":
        plugin.context.platform_manager.platform_insts = []

    overview = await controller._build_overview()
    group = next(group for group in overview["groups"] if group["umo"] == TG_GROUP)
    assert group["manually_added"] is True
    web.fake_request.payload = {"umo": TG_GROUP}
    before = copy.deepcopy(data)
    payload, status = await controller.remove_telegram_group()
    assert status == 409 and not payload.get("removed")
    assert data == before

    await service.remove(TG_GROUP, "tester")
    before = copy.deepcopy(data)
    payload, status = await controller.remove_telegram_group()
    assert status == 200 and payload["removed"] is True
    assert data["twitter_telegram_groups"] == {other: before["twitter_telegram_groups"][other]}
    assert data["twitter_subs"] == before["twitter_subs"]
    assert data["twitter_retweet_dedup_seen"] == before["twitter_retweet_dedup_seen"]
    overview = await controller._build_overview()
    assert TG_GROUP not in [group["umo"] for group in overview["groups"]]
    assert other in [group["umo"] for group in overview["groups"]]
    _payload, status = await controller.remove_telegram_group()
    assert status == 404
    # 可重新添加；存储刷新后删除结果仍保留。
    reloaded = make()
    assert TG_GROUP not in await reloaded.subscription_service.get_telegram_groups()
    await reloaded.subscription_service.register_telegram_group("my-bot", {"group_id": "-100123", "group_name": "重新添加"})
    assert TG_GROUP in await reloaded.subscription_service.get_telegram_groups()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    [], {}, {"umo": None}, {"umo": TG_PRIVATE}, {"umo": TG_TOPIC},
    {"umo": "qq-bot:GroupMessage:123"}, {"umo": "my-bot:GroupMessage:-0"},
    {"umo": "my-bot:GroupMessage:-" + "1" * 40},
])
async def test_remove_manual_group_rejects_invalid_request(plugin_env, payload, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    await plugin.subscription_service.register_telegram_group("my-bot", {"group_id": "-100123", "group_name": "保留群"})
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = payload
    before = copy.deepcopy(data)
    _payload, status = await controller.remove_telegram_group()
    assert status == 400 and data == before


@pytest.mark.asyncio
async def test_cannot_remove_discovered_group_or_treat_storage_failure_as_success(plugin_env, webui_module):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = webui_module
    controller = web.TwitterWebUIController(plugin, plugin.context)
    data["twitter_subs"] = {"tester": {"subscribers": {TG_GROUP: {}}}}
    web.fake_request.payload = {"umo": TG_GROUP}
    before = copy.deepcopy(data)
    payload, status = await controller.remove_telegram_group()
    assert status == 404 and not payload.get("removed") and data == before
    overview = await controller._build_overview()
    assert overview["groups"][0]["manually_added"] is False

    data["twitter_subs"] = {}
    await plugin.subscription_service.register_telegram_group("my-bot", {"group_id": "-100123", "group_name": "保留群"})
    before = copy.deepcopy(data)
    put = plugin.put_kv_data

    async def fail_put(_key, _value):
        raise OSError("secret token")

    plugin.put_kv_data = fail_put
    payload, status = await controller.remove_telegram_group()
    assert status == 500 and not payload.get("removed") and "secret" not in str(payload)
    assert data == before
    plugin.put_kv_data = put
    payload, status = await controller.remove_telegram_group()
    assert status == 200 and payload["removed"] is True


@pytest.mark.asyncio
async def test_remove_and_register_different_manual_groups_do_not_lose_updates(plugin_env):
    _module, make, _data, _calls, _fail = plugin_env
    service = make().subscription_service
    await service.register_telegram_group("my-bot", {"group_id": "-100123", "group_name": "待删除"})
    result, created = await asyncio.gather(
        service.remove_telegram_group(TG_GROUP),
        service.register_telegram_group("second-bot", {"group_id": "-100123", "group_name": "新增群"}),
    )
    assert result["ok"] and created
    assert set(await service.get_telegram_groups()) == {"second-bot:GroupMessage:-100123"}
