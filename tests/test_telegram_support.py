import asyncio
import copy
import sys
import types

import pytest
from test_provider_initialization import Image, Nodes, Plain, Video, _load_main_module
from test_twitter_webui import FakeContext, FakePlatform, FakePlugin, _load_webui_module

TG_GROUP = "my-bot:GroupMessage:-100123"
TG_TOPIC = "my-bot:GroupMessage:-100123#42"
TG_PRIVATE = "my-bot:FriendMessage:12345"


class TelegramClient:
    id = 9876

    def __init__(self, *, status="member", error=None, is_member=False, can_send_messages=False):
        self.status = status
        self.error = error
        self.is_member = is_member
        self.can_send_messages = can_send_messages
        self.calls = []

    async def get_chat(self, *, chat_id):
        self.calls.append(("chat", chat_id))
        if self.error:
            raise self.error
        return types.SimpleNamespace(type="supergroup", title="Telegram 测试群")

    async def get_chat_member(self, *, chat_id, user_id):
        self.calls.append(("member", chat_id, user_id))
        return types.SimpleNamespace(
            status=self.status, is_member=self.is_member,
            can_send_messages=self.can_send_messages,
        )


def telegram_platform(**options):
    platform = FakePlatform("my-bot", name="telegram")
    platform.client = TelegramClient(**options)
    return platform


@pytest.fixture
def plugin_env():
    previous_web = sys.modules.get("astrbot.api.web")
    module = _load_main_module()
    data = {}
    calls = []
    fail = set()

    async def get(key, default):
        return copy.deepcopy(data.get(key, default))

    async def put(key, value):
        data[key] = copy.deepcopy(value)

    async def send(umo, chain):
        calls.append((umo, chain.chain))
        # 模拟 Telegram 对 Nodes 的静默忽略，防止假成功测试掩盖问题。
        if umo.startswith("my-bot:"):
            assert not any(isinstance(part, Nodes) for part in chain.chain)
        return umo not in fail

    context = FakeContext([telegram_platform(), FakePlatform("qq-bot")])
    context.send_message = send

    def make(*, collective=False):
        plugin = module.TwitterPlugin(context, {
            "twitter_use_node": True,
            "twitter_collective_forward": collective,
            "twitter_data_provider": "fxtwitter",
        })
        plugin.get_kv_data = get
        plugin.put_kv_data = put
        plugin._provider_ready = True
        return plugin

    yield module, make, data, calls, fail
    if previous_web is None:
        sys.modules.pop("astrbot.api.web", None)
    else:
        sys.modules["astrbot.api.web"] = previous_web


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
    assert [type(part) for part in tg[0]] == [Plain, Image]
    assert isinstance(tg[1][0], Video)
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


def make_event(umo, message):
    return types.SimpleNamespace(
        unified_msg_origin=umo, message_str=message,
        get_platform_name=lambda: "telegram",
        plain_result=lambda text: text, chain_result=lambda chain: chain,
    )


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
    primary = next(item for item in results if isinstance(item, list))
    assert [type(part) for part in primary] == [Plain, Image]
    assert calls[0][0] == TG_TOPIC and isinstance(calls[0][1][0], Video)


@pytest.mark.asyncio
async def test_webui_discovers_existing_telegram_groups_and_topics_and_adds_author(plugin_env):
    web = _load_webui_module()
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
@pytest.mark.parametrize("status,error", [
    ("left", None), ("kicked", None), ("restricted", None),
    ("member", RuntimeError("URL contains secret token")),
])
async def test_webui_unavailable_telegram_keeps_subs_and_prevents_new_ones(plugin_env, status, error):
    web = _load_webui_module()
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
async def test_webui_cannot_create_unverified_topic_and_handles_empty_bot(plugin_env):
    web = _load_webui_module()
    plugin = FakePlugin()
    controller = web.TwitterWebUIController(plugin, FakeContext([telegram_platform()]))
    overview = await controller._build_overview()
    assert overview["groups"] == []
    assert overview["group_sources"][0]["available"] is True
    web.fake_request.payload = {"umo": TG_TOPIC, "username": "tester"}
    _payload, status = await controller.add_subscription()
    assert status == 400 and plugin.last_add is None


@pytest.mark.asyncio
async def test_webui_restricted_telegram_bot_requires_sending_permission(plugin_env):
    web = _load_webui_module()
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
async def test_manual_telegram_group_persists_without_subscriptions_and_is_idempotent(plugin_env):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = _load_webui_module()
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
async def test_manual_telegram_group_rejects_invalid_target_without_saving(plugin_env, payload):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = _load_webui_module()
    controller = web.TwitterWebUIController(plugin, plugin.context)
    web.fake_request.payload = payload
    result, status = await controller.add_telegram_group()
    assert status == 400 and not result.get("saved")
    assert data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["left", "kicked", "channel", "timeout", "network", "save"])
async def test_manual_telegram_group_failure_does_not_create_group(plugin_env, failure):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = _load_webui_module()
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
async def test_registered_empty_group_stays_visible_when_bot_leaves(plugin_env):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    web = _load_webui_module()
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
