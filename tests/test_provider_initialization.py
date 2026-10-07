import asyncio
import copy
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Component:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plain(_Component):
    def __init__(self, text):
        super().__init__(text=text)


class Image(_Component):
    @staticmethod
    def fromURL(url):
        return Image(file=url)

    @staticmethod
    def fromFileSystem(path):
        return Image(file=path)

    @staticmethod
    def fromBytes(data):
        return Image(data=data)


class Video(_Component):
    @staticmethod
    def fromURL(url):
        return Video(file=url)


class Node(_Component):
    pass


class Nodes(_Component):
    def __init__(self, nodes):
        super().__init__(nodes=nodes)


class MessageChain(_Component):
    pass


def _decorator(*_args, **_kwargs):
    return lambda func: func


class FakeTwitterAPI:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.provider = kwargs.get("provider", "nitter")
        self.nitter_url = kwargs.get("nitter_url", "")
        self.provider_ready = False
        self.fx_checks = 0
        self.nitter_checks = 0
        self.closed = False
        self.__class__.instances.append(self)

    @property
    def is_ready(self):
        if self.provider == "fxtwitter":
            return self.provider_ready
        return bool(self.nitter_url)

    async def check_fxtwitter_available(self):
        self.fx_checks += 1
        self.provider_ready = True
        return True

    async def check_website_available(self, websites):
        self.nitter_checks += 1
        self.nitter_url = websites[0] if websites else "https://nitter.test"
        self.provider_ready = True
        return self.nitter_url

    async def close(self):
        self.closed = True


class FakeFxTwitterTimelineError(RuntimeError):
    pass


def _load_main_module():
    package_name = "twitter_provider_test_package"
    for module_name in list(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            del sys.modules[module_name]

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    api.AstrBotConfig = dict
    api.logger = _Logger()
    event.AstrMessageEvent = object
    event.MessageChain = MessageChain
    event.filter = types.SimpleNamespace(
        command=_decorator,
        event_message_type=_decorator,
        permission_type=_decorator,
        EventMessageType=types.SimpleNamespace(ALL="all"),
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    components.Plain = Plain
    components.Image = Image
    components.Video = Video
    components.Node = Node
    components.Nodes = Nodes

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace()
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
    )

    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package

    twitter_api = types.ModuleType(f"{package_name}.twitter_api")
    twitter_api.DATA_PROVIDER_NITTER = "nitter"
    twitter_api.DATA_PROVIDER_FXTWITTER = "fxtwitter"
    twitter_api.DATA_PROVIDER_OPTIONS = ("nitter", "fxtwitter")
    twitter_api.DEFAULT_FXTWITTER_API_BASE = "https://api.fxtwitter.com"
    twitter_api.FxTwitterTimelineError = FakeFxTwitterTimelineError
    twitter_api.TwitterAPI = FakeTwitterAPI
    twitter_api.WEBSITE_LIST = ["https://nitter.test"]
    twitter_api.get_next_website = lambda *_args, **_kwargs: None
    sys.modules[twitter_api.__name__] = twitter_api

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin_module():
    FakeTwitterAPI.instances.clear()
    return _load_main_module()


@pytest.fixture
def subscription_list_query(plugin_module):
    def make_query(count, name="x", flags=False):
        store = {
            f"u{i}": {
                "screen_name": name,
                "subscribers": {
                    "session": {"status": not flags, "r18": flags, "media": flags}
                },
            }
            for i in range(count)
        }
        store["private_only"] = {"subscribers": {"private": {}}}
        original = copy.deepcopy(store)

        async def get_kv(key, default):
            assert key == "twitter_subs"
            return store

        async def put_kv(*_args):
            pytest.fail("查询列表不得写入 KV")

        plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
        plugin.subscription_service = plugin_module.SubscriptionService(
            get_kv, put_kv, object(), lambda: False
        )

        async def query(umo="session"):
            event = types.SimpleNamespace(
                unified_msg_origin=umo,
                plain_result=lambda text: text,
                chain_result=lambda chain: MessageChain(chain=chain),
                get_self_id=lambda: "123456789",
            )
            results = [result async for result in plugin.list_follows(event)]
            assert len(results) == 1
            assert store == original
            return results[0]

        return query

    return make_query


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 50, 51, 104, 115])
async def test_subscription_list_forward_is_complete(subscription_list_query, count):
    query = subscription_list_query(count)
    result = await query()
    assert isinstance(result, MessageChain)
    assert len(result.chain) == 1
    assert isinstance(result.chain[0], Nodes)
    nodes = result.chain[0].nodes
    seen = []
    for index, node in enumerate(nodes, 1):
        assert isinstance(node, Node)
        assert node.uin == "123456789"
        assert node.name == "推特订阅列表"
        assert len(node.content) == 1
        assert isinstance(node.content[0], Plain)
        text = node.content[0].text
        assert len(text) <= 1000
        assert f"共 {count} 个，第 {index}/{len(nodes)} 段" in text
        rows = re.findall(r"^(\d+)\. 🟢 @(\w+) \(x\)$", text, re.MULTILINE)
        assert 1 <= len(rows) <= 50
        seen.extend(rows)
        assert "下一页" not in text
        assert "private_only" not in text
    assert seen == [(str(i + 1), f"u{i}") for i in range(count)]
    private_result = await query(umo="private")
    private_nodes = private_result.chain[0].nodes
    assert len(private_nodes) == 1
    assert "@private_only (private_only)" in private_nodes[0].content[0].text
    if count == 50:
        assert len(nodes) == 1
    if count == 51:
        assert len(nodes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["测试\n\t 用户  名", "测😀" * 80])
async def test_subscription_list_limits_long_names(subscription_list_query, name):
    query = subscription_list_query(115, name, flags=True)
    result = await query()
    assert isinstance(result, MessageChain)
    nodes = result.chain[0].nodes
    display_name = " ".join(name.split())
    if len(display_name) > 50:
        display_name = display_name[:49] + "…"
    rows = []
    for node in nodes:
        text = node.content[0].text
        assert len(text) <= 1000
        rows.extend(line for line in text.splitlines() if re.match(r"\d+\. ", line))
    assert rows == [
        f"{i + 1}. 🔴 @u{i} ({display_name}) | R18 | 仅媒体"
        for i in range(115)
    ]
    assert len(re.findall(r"^\d+\. ", nodes[0].content[0].text, re.MULTILINE)) < 50


@pytest.mark.asyncio
async def test_subscription_list_empty(subscription_list_query):
    assert await subscription_list_query(0)() == "当前没有订阅任何推主"


async def _wait_forever():
    await asyncio.Event().wait()


def _delivery_contract(plugin_module):
    return sys.modules[
        f"{plugin_module.__package__}.services.tweet_delivery_service"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_node,collective", [(False, False), (True, False), (True, True)]
)
async def test_false_send_retains_cursor_and_recovers(
    plugin_module, use_node, collective
):
    store = {
        "twitter_subs": {
            "tester": {
                "since_id": "100",
                "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
            }
        },
        "twitter_retweet_dedup_seen": {},
    }
    recovered = False
    calls = []

    def texts(chain):
        for part in chain:
            if isinstance(part, Nodes):
                for node in part.nodes:
                    yield from texts(node.content)
            elif isinstance(part, Plain):
                yield part.text

    async def send_message(umo, message):
        ids = [
            tweet_id for tweet_id in ("101", "102", "103")
            if any(f"status/{tweet_id}" in text for text in texts(message.chain))
        ]
        calls.append((umo, ids))
        return recovered or umo == "good" or ids == ["101"]

    async def get_kv(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_kv(key, value):
        store[key] = copy.deepcopy(value)

    async def timeline(_username, since_id):
        return [
            {
                "tweet_id": str(i), "username": "original", "is_retweet": True,
                "retweeter_username": "tester",
            }
            for i in range(101, 104) if i > int(since_id)
        ]

    async def get_tweet(username, tweet_id):
        return {
            "status": True, "tweet_id": tweet_id, "username": username, "text": "body"
        }

    plugin = plugin_module.TwitterPlugin(
        types.SimpleNamespace(send_message=send_message),
        {
            "twitter_use_node": use_node,
            "twitter_collective_forward": collective,
            "twitter_deduplicate_retweets": True,
        },
    )
    plugin.get_kv_data = get_kv
    plugin.put_kv_data = put_kv
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = get_tweet
    polling = plugin.polling_service
    await polling.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    if collective:
        assert store["twitter_subs"]["tester"]["since_id"] == "100"
        assert store["twitter_retweet_dedup_seen"] == {}
        await polling.flush_pending_collective()
        assert not plugin.delivery_service.has_collected
        assert not polling.has_pending_collective

    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == ("100" if collective else "101")
    assert author.get("processed_tweet_ids", []) == ([] if collective else ["101"])
    assert store["twitter_retweet_dedup_seen"]["bad"] == ["101"]
    assert store["twitter_retweet_dedup_seen"]["good"] == (
        ["101", "102", "103"] if collective else ["101", "102"]
    )
    assert all(ids for _, ids in calls)
    histories = {umo: [item["tweet_id"] for item in config.get("recent_deliveries", [])]
                 for umo, config in author["subscribers"].items()}
    assert histories["bad"] == ["101"]
    assert histories["good"] == (["103", "102", "101"] if collective else ["102", "101"])
    if not collective:
        assert all("103" not in ids for _, ids in calls)

    recovered = True
    calls.clear()
    await polling.check_user("tester", copy.deepcopy(author))
    await polling.flush_pending_collective()
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "103"
    assert author["processed_tweet_ids"] == ["101", "102", "103"]
    assert store["twitter_retweet_dedup_seen"]["bad"] == ["101", "102", "103"]
    assert not any(umo == "good" and "102" in ids for umo, ids in calls)
    assert any(umo == "bad" and "102" in ids for umo, ids in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,fail_all", [
    ("plain", True), ("node", True), ("collective", True), ("collective", False),
])
@pytest.mark.parametrize("data_provider", ["nitter", "fxtwitter"])
@pytest.mark.parametrize("history_available", [True, False])
async def test_partial_delivery_retries_only_failed_sessions_after_reload(
    plugin_module, monkeypatch, transport, fail_all, data_provider, history_available
):
    # Legacy subscriptions have neither delivery history nor retry receipts.
    store = {"twitter_subs": {"tester": {
        "since_id": "100",
        "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    tweet_ids = [str(i) for i in range(101, 108)]
    timeline_ids = list(tweet_ids)
    recovered = False
    delivered = {"good": [], "bad": []}
    attempted = {"good": 0, "bad": 0}

    def texts(chain):
        for part in chain:
            if isinstance(part, Nodes):
                for node in part.nodes:
                    yield from texts(node.content)
            elif isinstance(part, Plain):
                yield part.text

    async def send_message(umo, message):
        attempted[umo] += 1
        content = "\n".join(texts(message.chain))
        ids = [tweet_id for tweet_id in timeline_ids if f"status/{tweet_id}" in content]
        if umo == "bad" and not recovered and (fail_all or "101" in ids):
            return False
        delivered[umo].extend(ids)
        return True

    async def get_kv(key, default):
        return store.get(key, default)

    async def put_kv(key, value):
        store[key] = copy.deepcopy(value)

    async def timeline(_username, since_id):
        return [{"tweet_id": tweet_id, "username": "tester"}
                for tweet_id in timeline_ids if int(tweet_id) > int(since_id)]

    async def get_tweet(_username, tweet_id):
        return {"status": True, "tweet_id": tweet_id, "text": "body"}

    if not history_available:
        def unavailable_history(*_args):
            raise RuntimeError("history unavailable")

        monkeypatch.setattr(
            plugin_module.SubscriptionService, "prepare_delivery", unavailable_history
        )

    def reload_plugin(provider):
        plugin = plugin_module.TwitterPlugin(
            types.SimpleNamespace(send_message=send_message), {
                "twitter_data_provider": provider,
                "twitter_use_node": transport != "plain",
                "twitter_collective_forward": transport == "collective",
                "twitter_poll_max_tweets_per_user": 7,
                "twitter_deduplicate_retweets": False,
            },
        )
        plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = get_tweet
        return plugin

    async def poll(plugin):
        author = copy.deepcopy(store["twitter_subs"]["tester"])
        assert await plugin.polling_service.check_user("tester", author)
        await plugin.polling_service.flush_pending_collective()

    await poll(reload_plugin(data_provider))
    first_delivered = list(delivered["good"])
    first_attempted = dict(attempted)
    assert first_delivered == (tweet_ids if transport == "collective" else ["101"])
    assert delivered["bad"] == ([] if fail_all else tweet_ids[1:])
    assert store["twitter_subs"]["tester"]["since_id"] == "100"

    # Repeated failures and a source switch must preserve successful sessions.
    other_provider = "nitter" if data_provider == "fxtwitter" else "fxtwitter"
    if not fail_all:
        # Later successes must not let one failed item grow the retry window.
        timeline_ids.extend(str(i) for i in range(108, 115))
    plugin = reload_plugin(other_provider)
    for _ in range(2):
        await poll(plugin)
        assert delivered["good"] == first_delivered
        assert attempted["good"] == first_attempted["good"]
        assert store["twitter_subs"]["tester"]["since_id"] == "100"
    assert attempted["bad"] > first_attempted["bad"]

    recovered = True
    await poll(reload_plugin(data_provider))
    assert delivered["good"] == tweet_ids
    assert sorted(delivered["bad"], key=int) == tweet_ids
    assert store["twitter_subs"]["tester"]["since_id"] == "107"
    await poll(reload_plugin(other_provider))
    assert delivered["good"] == timeline_ids
    assert sorted(delivered["bad"], key=int) == timeline_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
async def test_delivery_receipts_restore_retweet_dedup_after_write_failure(plugin_module, transport):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {"group": {"status": True}},
    }}, "twitter_retweet_dedup_seen": {}}
    sent = []
    fail_seen_write = True

    async def get_kv(key, default):
        return store.get(key, default)

    async def put_kv(key, value):
        nonlocal fail_seen_write
        if key == "twitter_retweet_dedup_seen" and fail_seen_write:
            fail_seen_write = False
            raise OSError("dedup KV unavailable")
        store[key] = copy.deepcopy(value)

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    async def timeline(_username, since_id):
        return [{"tweet_id": "101", "username": "original", "is_retweet": True,
                 "retweeter_username": "tester"}] if since_id == "100" else []

    async def get_tweet(_username, tweet_id):
        return {"status": True, "tweet_id": tweet_id, "text": "body"}

    def reload_plugin():
        plugin = plugin_module.TwitterPlugin(types.SimpleNamespace(send_message=send_message), {
            "twitter_use_node": transport != "plain",
            "twitter_collective_forward": transport == "collective",
            "twitter_deduplicate_retweets": True,
        })
        plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = get_tweet
        return plugin

    for attempt in range(2):
        plugin = reload_plugin()
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()
        assert sent == ["group"]
        assert store["twitter_subs"]["tester"]["since_id"] == ("100" if attempt == 0 else "101")
    assert store["twitter_retweet_dedup_seen"] == {"group": ["101"]}


@pytest.mark.asyncio
async def test_fxtwitter_initialization_skips_nitter(plugin_module):
    config = {
        "basic": {
            "twitter_data_provider": "fxtwitter",
            "twitter_fxtwitter_api_base": "https://api.fxtwitter.com/",
            "twitter_nitter_url": "https://must-not-be-used.example",
        }
    }
    plugin = plugin_module.TwitterPlugin(object(), config)
    plugin._poll_tweets = _wait_forever

    assert plugin.website_list == []
    assert plugin.fxtwitter_api_base == "https://api.fxtwitter.com"
    await plugin.initialize()

    fake = FakeTwitterAPI.instances[-1]
    assert fake.fx_checks == 1
    assert fake.nitter_checks == 0
    assert plugin._provider_ready is True
    assert plugin._poll_task is not None

    await plugin.terminate()
    assert fake.closed is True


def test_fxtwitter_readiness_stays_in_sync_with_api(plugin_module):
    plugin = plugin_module.TwitterPlugin(
        object(),
        {"basic": {"twitter_data_provider": "fxtwitter"}},
    )

    plugin.twitter_api.provider_ready = True
    assert plugin._provider_ready is True

    plugin.twitter_api.provider_ready = False
    assert plugin._provider_ready is False

    plugin._provider_ready = True
    assert plugin.twitter_api.is_ready is True


@pytest.mark.asyncio
async def test_fxtwitter_initialization_failure_keeps_recovery_task(
    plugin_module,
    monkeypatch,
):
    async def unavailable(self):
        self.fx_checks += 1
        self.provider_ready = False
        return False

    monkeypatch.setattr(
        FakeTwitterAPI,
        "check_fxtwitter_available",
        unavailable,
    )
    plugin = plugin_module.TwitterPlugin(
        object(),
        {"basic": {"twitter_data_provider": "fxtwitter"}},
    )
    plugin._poll_tweets = _wait_forever

    await plugin.initialize()

    fake = FakeTwitterAPI.instances[-1]
    assert fake.fx_checks == 1
    assert plugin._provider_ready is False
    assert plugin._running is True
    assert plugin._poll_task is not None

    await plugin.terminate()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_fxtwitter_health_exception_keeps_recovery_task(
    plugin_module,
    monkeypatch,
):
    async def unavailable(self):
        self.fx_checks += 1
        self.provider_ready = False
        raise RuntimeError("proxy unavailable")

    monkeypatch.setattr(
        FakeTwitterAPI,
        "check_fxtwitter_available",
        unavailable,
    )
    plugin = plugin_module.TwitterPlugin(
        object(),
        {"basic": {"twitter_data_provider": "fxtwitter"}},
    )
    plugin._poll_tweets = _wait_forever

    await plugin.initialize()

    fake = FakeTwitterAPI.instances[-1]
    assert fake.fx_checks == 1
    assert plugin._provider_ready is False
    assert plugin._running is True
    assert plugin._poll_task is not None

    await plugin.terminate()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_fxtwitter_polling_retries_until_recovered(plugin_module):
    plugin = plugin_module.TwitterPlugin(
        object(),
        {"basic": {"twitter_data_provider": "fxtwitter"}},
    )
    fake = FakeTwitterAPI.instances[-1]
    plugin._provider_ready = False
    fake.provider_ready = False
    plugin._running = True
    waits = 0
    polls = 0
    health_results = iter((False, True))

    async def wait_for_next_poll():
        nonlocal waits
        waits += 1

    async def recover():
        fake.fx_checks += 1
        fake.provider_ready = next(health_results)
        return fake.provider_ready

    async def check_all():
        nonlocal polls
        polls += 1
        plugin._running = False

    plugin._wait_for_next_poll = wait_for_next_poll
    plugin.twitter_api.check_fxtwitter_available = recover
    plugin._check_all_subscriptions = check_all

    await plugin._poll_tweets()

    assert waits == 2
    assert fake.fx_checks == 2
    assert polls == 1
    assert plugin._provider_ready is True


@pytest.mark.asyncio
async def test_nitter_default_preserves_original_initialization(plugin_module):
    plugin = plugin_module.TwitterPlugin(object(), {})
    plugin._poll_tweets = _wait_forever

    assert plugin.data_provider == "nitter"
    assert plugin.website_list == ["https://nitter.test"]
    await plugin.initialize()

    fake = FakeTwitterAPI.instances[-1]
    assert fake.nitter_checks == 1
    assert fake.fx_checks == 0
    assert plugin._provider_ready is True

    await plugin.terminate()


@pytest.mark.asyncio
async def test_nitter_initialization_failure_still_skips_polling(
    plugin_module,
    monkeypatch,
):
    async def unavailable(self, _websites):
        self.nitter_checks += 1
        self.nitter_url = ""
        self.provider_ready = False
        return None

    monkeypatch.setattr(
        FakeTwitterAPI,
        "check_website_available",
        unavailable,
    )
    plugin = plugin_module.TwitterPlugin(object(), {})
    plugin._poll_tweets = _wait_forever

    await plugin.initialize()

    fake = FakeTwitterAPI.instances[-1]
    assert fake.nitter_checks == 1
    assert plugin._provider_ready is False
    assert plugin._running is False
    assert plugin._poll_task is None

    await plugin.terminate()
    assert fake.closed is True


def test_flat_and_grouped_provider_config_are_compatible(plugin_module):
    assert plugin_module.TwitterWebUIController is None

    flat = plugin_module.TwitterPlugin(
        object(),
        {
            "twitter_data_provider": "fxtwitter",
            "twitter_fxtwitter_api_base": "https://fx.example/",
            "twitter_poll_max_tweets_per_user": 7,
            "twitter_link_recognition_enabled": False,
        },
    )
    grouped = plugin_module.TwitterPlugin(
        object(),
        {
            "basic": {
                "twitter_data_provider": "fxtwitter",
                "twitter_fxtwitter_api_base": "https://grouped.example/",
                "twitter_poll_max_tweets_per_user": 9,
            },
            "content_filter": {
                "twitter_link_recognition_enabled": "command",
            },
        },
    )

    assert flat.data_provider == "fxtwitter"
    assert flat.fxtwitter_api_base == "https://fx.example"
    assert flat.poll_max_tweets_per_user == 7
    assert flat.link_recognition_mode == "off"
    assert grouped.data_provider == "fxtwitter"
    assert grouped.fxtwitter_api_base == "https://grouped.example"
    assert grouped.poll_max_tweets_per_user == 9
    assert grouped.link_recognition_mode == "command"

    defaulted = plugin_module.TwitterPlugin(object(), {})
    assert defaulted.poll_max_tweets_per_user == 5
    assert defaulted.link_recognition_mode == "auto"


@pytest.mark.parametrize("config,expected", [
    ({}, 60),
    ({"twitter_translate_timeout_seconds": 15}, 15),
    ({"translation": {"twitter_translate_timeout_seconds": 90}}, 90),
    ({"translation": {"twitter_translate_timeout_seconds": 0}}, 1),
    ({"twitter_translate_timeout_seconds": -2}, 1),
])
def test_translation_timeout_config_compatibility(plugin_module, config, expected):
    plugin = plugin_module.TwitterPlugin(object(), config)
    assert plugin.translate_timeout_seconds == expected
    assert plugin.message_service.settings.translate_timeout_seconds == expected
    assert plugin.message_service.avatar_cache is not None
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    item = schema["translation"]["items"]["twitter_translate_timeout_seconds"]
    assert item["type"] == "int" and item["default"] == 60


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "auto"),
        (False, "off"),
        ("true", "auto"),
        ("false", "off"),
        ("auto", "auto"),
        ("off", "off"),
        ("command", "command"),
        ("unexpected", "auto"),
    ],
)
def test_link_recognition_mode_normalization(plugin_module, value, expected):
    assert plugin_module._normalize_link_recognition_mode(value) == expected


def test_schema_exposes_link_modes_and_provider_selector():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    link_mode = schema["content_filter"]["items"][
        "twitter_link_recognition_enabled"
    ]
    provider = schema["translation"]["items"][
        "twitter_translate_provider_id"
    ]

    assert link_mode["type"] == "string"
    assert link_mode["default"] == "auto"
    assert link_mode["options"] == ["auto", "off", "command"]
    assert link_mode["labels"] == [
        "开启（自动解析）",
        "关闭",
        "开启但仅指令触发",
    ]
    assert provider["_special"] == "select_provider"


@pytest.mark.asyncio
async def test_detail_failure_only_advances_cursor_to_last_success(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {"tweet_id": "101", "username": "tester", "is_retweet": False},
                {"tweet_id": "102", "username": "tester", "is_retweet": False},
            ]

        async def get_tweet(self, _username, tweet_id):
            if tweet_id == "101":
                return {
                    "status": True,
                    "tweet_id": "101",
                    "username": "tester",
                    "text": "ok",
                }
            return {"status": False, "tweet_id": "102", "username": "tester"}

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        saved = copy.deepcopy(data)
        store.clear()
        store.update(saved)

    class Delivery:
        async def push_to_subscribers(self, *_args, **_kwargs):
            delivery_module = sys.modules[
                f"{plugin_module.__package__}.services.tweet_delivery_service"
            ]
            return delivery_module.DeliveryResult(
                delivery_module.DeliveryState.DELIVERED
            )

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        api,
        lambda: True,
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    result = await polling.check_user("tester", store["tester"])

    assert result is False
    assert store["tester"]["since_id"] == "101"


@pytest.mark.asyncio
async def test_timeline_failure_does_not_advance_polling_cursor(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }
    save_calls = 0

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            raise plugin_module.FxTwitterTimelineError("第二页请求失败")

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, _data):
        nonlocal save_calls
        save_calls += 1

    class Delivery:
        async def push_to_subscribers(self, *_args, **_kwargs):
            return None

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        api,
        lambda: True,
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    result = await polling.check_user("tester", store["tester"])

    assert result is False
    assert store["tester"]["since_id"] == "100"
    assert save_calls == 0


@pytest.mark.asyncio
async def test_fxtwitter_global_failure_stops_remaining_users(plugin_module):
    calls = []

    class API:
        is_ready = True

        async def get_user_timeline_items(self, username, _since_id):
            calls.append(username)
            self.is_ready = False
            raise plugin_module.FxTwitterTimelineError("代理连接失败")

    class Subscriptions:
        @staticmethod
        def processed_tweet_ids(_info):
            return set()

        async def get_all(self):
            return {
                "first": {"since_id": "100"},
                "second": {"since_id": "200"},
            }

    class Delivery:
        collective_enabled = False

    polling = plugin_module.PollingService(
        API(),
        Subscriptions(),
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    await polling.check_all()

    assert calls == ["first"]


def test_timeline_metadata_is_the_only_source_of_retweet_context(plugin_module):
    tweet_info = {
        "username": "original",
        "retweet": {
            "retweeter_username": "stale",
            "retweeter_screen_name": "Stale",
        },
    }

    plugin_module.TwitterPlugin._attach_timeline_item_metadata(
        tweet_info,
        {"username": "original", "is_retweet": False},
    )
    assert tweet_info["retweet"] is None

    plugin_module.TwitterPlugin._attach_timeline_item_metadata(
        tweet_info,
        {
            "username": "original",
            "is_retweet": True,
            "retweeter_username": "tester",
            "retweeter_screen_name": "Tester",
        },
    )
    assert tweet_info["retweet"] == {
        "retweeter_username": "tester",
        "retweeter_screen_name": "Tester",
    }


@pytest.mark.asyncio
async def test_commands_report_timeline_failures_clearly(plugin_module):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin._provider_ready = True
    store = {}

    class API:
        async def get_user_info(self, _username):
            return {
                "status": True,
                "screen_name": "Tester",
                "bio": "",
                "user_name": "tester",
            }

        async def get_user_newtimeline(self, _username):
            raise plugin_module.FxTwitterTimelineError("首页请求失败")

        async def get_user_timeline_items(self, _username):
            raise plugin_module.FxTwitterTimelineError("首页请求失败")

    class Event:
        message_str = "/推特关注 tester"
        unified_msg_origin = "session"

        @staticmethod
        def plain_result(text):
            return text

    api = API()
    plugin.twitter_api = api

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    plugin.subscription_service = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        api,
        lambda: True,
    )
    event = Event()

    follow_results = [
        result async for result in plugin.follow_twitter(event, "tester")
    ]
    test_results = [result async for result in plugin.test_tweet(event, "tester")]

    assert follow_results == ["获取 @tester 时间线失败，请稍后重试"]
    assert test_results[-1] == "获取 @tester 时间线失败，请稍后重试"


@pytest.mark.asyncio
async def test_existing_author_subscription_reuses_cursor_without_api_calls(
    plugin_module,
):
    store = {
        "Tester": {
            "screen_name": "Tester Name",
            "since_id": "500",
            "subscribers": {
                "bot:GroupMessage:1": {"status": True, "r18": False, "media": False}
            },
        }
    }

    class API:
        async def get_user_info(self, _username):
            raise AssertionError("不应重新请求已有推主资料")

        async def get_user_newtimeline(self, _username):
            raise AssertionError("不应重新请求已有推主时间线")

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    service = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        API(),
        lambda: False,
    )

    result = await service.add(
        "bot:GroupMessage:2",
        "tester",
        r18=True,
        media_only=True,
    )

    assert result["ok"] is True
    assert result["created_author"] is False
    assert store["Tester"]["since_id"] == "500"
    assert store["Tester"]["subscribers"]["bot:GroupMessage:2"] == {
        "status": True,
        "r18": True,
        "media": True,
    }


@pytest.mark.asyncio
async def test_poll_interval_save_wakes_timer_and_rolls_back_on_failure(
    plugin_module,
):
    class Config(dict):
        def __init__(self, fail=False):
            super().__init__({"basic": {"twitter_poll_interval": 5, "keep": "value"}})
            self.fail = fail
            self.save_calls = 0

        def save_config(self):
            self.save_calls += 1
            if self.fail:
                raise RuntimeError("disk full")

    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.config = Config()
    plugin.poll_interval = 5
    plugin._poll_wakeup = asyncio.Event()

    await plugin._set_poll_interval(9)

    assert plugin.config["basic"] == {
        "twitter_poll_interval": 9,
        "keep": "value",
    }
    assert plugin.poll_interval == 9
    assert plugin._poll_wakeup.is_set()

    failing = Config(fail=True)
    plugin.config = failing
    plugin.poll_interval = 5
    plugin._poll_wakeup.clear()

    with pytest.raises(RuntimeError, match="disk full"):
        await plugin._set_poll_interval(11)

    assert plugin.config["basic"] == {
        "twitter_poll_interval": 5,
        "keep": "value",
    }
    assert plugin.poll_interval == 5
    assert not plugin._poll_wakeup.is_set()


@pytest.mark.asyncio
async def test_subscription_service_concurrent_adds_preserve_sessions(
    plugin_module,
):
    store = {}

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class API:
        async def get_user_info(self, _username):
            await asyncio.sleep(0)
            return {"status": True, "screen_name": "Tester", "bio": ""}

        async def get_user_newtimeline(self, _username):
            await asyncio.sleep(0)
            return ["500"]

    service = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        API(),
        lambda: True,
    )

    first, second = await asyncio.gather(
        service.add("bot:GroupMessage:1", "tester"),
        service.add("bot:GroupMessage:2", "Tester", r18=True),
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(store) == 1
    author = next(iter(store.values()))
    assert set(author["subscribers"]) == {
        "bot:GroupMessage:1",
        "bot:GroupMessage:2",
    }
    assert author["since_id"] == "500"


def test_retweet_dedup_cache_is_bounded(plugin_module):
    seen_data = {}
    for tweet_id in range(505):
        plugin_module.SubscriptionService.mark_retweet_seen(
            seen_data,
            "session",
            str(tweet_id),
        )

    assert len(seen_data["session"]) == 500
    assert seen_data["session"][0] == "5"

    plugin_module.SubscriptionService.mark_retweet_seen(
        seen_data,
        "session",
        "100",
    )
    assert len(seen_data["session"]) == 500
    assert seen_data["session"][-1] == "100"


@pytest.mark.asyncio
async def test_polling_skips_disabled_retweets_and_advances_cursor(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {
                    "tweet_id": "101",
                    "username": "tester",
                    "is_retweet": True,
                }
            ]

        async def get_tweet(self, *_args):
            raise AssertionError("关闭转帖后不应请求转帖详情")

    sent = []

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    # 使用真实发送服务验证“不发送”，允许它先完成已有回执的恢复。
    plugin = plugin_module.TwitterPlugin(types.SimpleNamespace(send_message=send_message), {
        "twitter_include_retweets": False,
    })
    plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv
    api = API()
    plugin.twitter_api.get_user_timeline_items = api.get_user_timeline_items
    plugin.twitter_api.get_tweet = api.get_tweet
    assert await plugin.polling_service.check_user("tester", store["tester"]) is True
    assert store["tester"]["since_id"] == "101"
    assert sent == []


@pytest.mark.asyncio
async def test_test_command_and_link_recognition_share_prepared_delivery(
    plugin_module,
):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin._provider_ready = True
    plugin.include_retweets = True
    plugin.link_recognition_mode = plugin_module.LINK_RECOGNITION_MODE_AUTO

    class API:
        async def get_user_timeline_items(self, _username):
            return [
                {
                    "tweet_id": "123",
                    "username": "tester",
                    "is_retweet": False,
                }
            ]

        async def get_tweet(self, _username, _tweet_id):
            return {
                "status": True,
                "tweet_id": "123",
                "username": "tester",
                "screen_name": "Tester",
                "text": "tweet",
            }

    class Messages:
        async def maybe_translate(self, _tweet_info, _umo, cycle=None):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("tweet")]

        @staticmethod
        def build_author_display(_username, _screen_name):
            return "@tester (Tester)"

    class Delivery:
        def __init__(self):
            self.prepare_calls = 0

        def prepare_event_delivery(self, _chain, _nickname):
            self.prepare_calls += 1
            return types.SimpleNamespace(
                primary_chain=[Plain("prepared")],
                videos=[],
            )

        async def send_prepared_videos(self, _umo, _videos):
            return None

    class Event:
        unified_msg_origin = "session"

        def __init__(self, message_str):
            self.message_str = message_str
            self.stopped = False

        def stop_event(self):
            self.stopped = True

        @staticmethod
        def plain_result(text):
            return text

        @staticmethod
        def chain_result(chain):
            return chain

    plugin.twitter_api = API()
    plugin.message_service = Messages()
    plugin.delivery_service = Delivery()

    test_results = [
        result
        async for result in plugin.test_tweet(
            Event("/推特测试 tester"),
            "tester",
        )
    ]
    link_results = [
        result
        async for result in plugin.on_message(
            Event("https://x.com/tester/status/123")
        )
    ]
    command_event = Event(
        "/推特解析 https://x.com/tester/status/123"
    )
    command_results = [
        result
        async for result in plugin.parse_tweet_link(command_event)
    ]
    duplicate_results = [
        result
        async for result in plugin.on_message(
            Event("/推特解析 https://x.com/tester/status/123")
        )
    ]

    assert test_results[-1][0].text == "prepared"
    assert link_results[0][0].text == "prepared"
    assert command_results[0][0].text == "prepared"
    assert command_event.stopped is True
    assert duplicate_results == []
    assert plugin.delivery_service.prepare_calls == 3


@pytest.mark.asyncio
async def test_link_recognition_command_mode_and_off_mode(plugin_module):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin._provider_ready = True
    handled = []

    async def handle_link(_event, match, *, report_errors):
        handled.append((match.group(3), report_errors))
        yield "parsed"

    class Event:
        unified_msg_origin = "session"

        def __init__(self, message_str):
            self.message_str = message_str
            self.stopped = False

        def stop_event(self):
            self.stopped = True

        @staticmethod
        def plain_result(text):
            return text

    plugin._handle_tweet_link = handle_link
    plugin.link_recognition_mode = plugin_module.LINK_RECOGNITION_MODE_COMMAND

    automatic_results = [
        result
        async for result in plugin.on_message(
            Event("https://x.com/tester/status/123")
        )
    ]
    alias_event = Event(
        "/twitter_parse https://twitter.com/tester/status/123"
    )
    command_results = [
        result
        async for result in plugin.parse_tweet_link(alias_event)
    ]
    missing_link_results = [
        result
        async for result in plugin.parse_tweet_link(Event("/推特解析"))
    ]

    assert automatic_results == []
    assert command_results == ["parsed"]
    assert alias_event.stopped is True
    assert handled == [("123", True)]
    assert "用法" in missing_link_results[0]

    plugin.link_recognition_mode = plugin_module.LINK_RECOGNITION_MODE_OFF
    off_event = Event("/推特解析 https://x.com/tester/status/456")
    off_results = [
        result
        async for result in plugin.parse_tweet_link(off_event)
    ]

    assert off_results == ["推文链接解析已关闭"]
    assert off_event.stopped is True
    assert handled == [("123", True)]


@pytest.mark.asyncio
async def test_polling_limits_each_author_and_continues_next_round(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }
    saved_cursors = []
    delivery_contract = _delivery_contract(plugin_module)

    class API:
        async def get_user_timeline_items(self, _username, since_id):
            return [
                {
                    "tweet_id": str(tweet_id),
                    "username": "tester",
                    "is_retweet": False,
                }
                for tweet_id in range(int(since_id) + 1, 108)
            ]

        async def get_tweet(self, _username, tweet_id):
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": "tester",
                "text": tweet_id,
            }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))
        saved_cursors.append(store["tester"]["since_id"])

    class Delivery:
        collective_enabled = False

        async def push_to_subscribers(self, *_args, **_kwargs):
            return delivery_contract.DeliveryResult(
                delivery_contract.DeliveryState.DELIVERED
            )

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv, put_kv, api, lambda: True
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
            max_tweets_per_user=5,
        ),
    )

    assert await polling.check_user("tester", copy.deepcopy(store["tester"]))
    assert store["tester"]["since_id"] == "105"
    assert saved_cursors == ["101", "102", "103", "104", "105"]

    assert await polling.check_user("tester", copy.deepcopy(store["tester"]))
    assert store["tester"]["since_id"] == "107"
    assert saved_cursors[-2:] == ["106", "107"]


@pytest.mark.asyncio
async def test_skipped_deliveries_do_not_consume_poll_limit(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }
    delivery_contract = _delivery_contract(plugin_module)
    states = [
        delivery_contract.DeliveryState.SKIPPED,
        delivery_contract.DeliveryState.SKIPPED,
        *([delivery_contract.DeliveryState.DELIVERED] * 5),
    ]

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {
                    "tweet_id": str(tweet_id),
                    "username": "tester",
                    "is_retweet": False,
                }
                for tweet_id in range(101, 108)
            ]

        async def get_tweet(self, _username, tweet_id):
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": "tester",
            }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class Delivery:
        collective_enabled = False

        async def push_to_subscribers(self, *_args, **_kwargs):
            return delivery_contract.DeliveryResult(states.pop(0))

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv, put_kv, api, lambda: True
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="nitter",
            custom_nitter_url="https://nitter.example",
            website_list=(),
            max_tweets_per_user=5,
        ),
    )

    assert await polling.check_user("tester", store["tester"])
    assert store["tester"]["since_id"] == "107"
    assert states == []


@pytest.mark.asyncio
async def test_delivery_failure_stops_without_skipping_cursor(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }
    delivery_contract = _delivery_contract(plugin_module)
    attempted = []

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {
                    "tweet_id": str(tweet_id),
                    "username": "tester",
                    "is_retweet": False,
                }
                for tweet_id in range(101, 104)
            ]

        async def get_tweet(self, _username, tweet_id):
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": "tester",
            }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class Delivery:
        collective_enabled = False

        async def push_to_subscribers(self, _username, tweet_info, cycle=None, **_kwargs):
            attempted.append(tweet_info["tweet_id"])
            state = (
                delivery_contract.DeliveryState.FAILED
                if tweet_info["tweet_id"] == "102"
                else delivery_contract.DeliveryState.DELIVERED
            )
            return delivery_contract.DeliveryResult(state)

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv, put_kv, api, lambda: True
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    assert await polling.check_user("tester", store["tester"])
    assert attempted == ["101", "102"]
    assert store["tester"]["since_id"] == "101"


@pytest.mark.asyncio
@pytest.mark.parametrize("flush_succeeds", [True, False])
async def test_collective_cursor_waits_for_flush(
    plugin_module,
    flush_succeeds,
):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {"session": {"status": True}},
        }
    }
    delivery_contract = _delivery_contract(plugin_module)

    class API:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {
                    "tweet_id": "101",
                    "username": "tester",
                    "is_retweet": False,
                }
            ]

        async def get_tweet(self, _username, tweet_id):
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": "tester",
            }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class Delivery:
        collective_enabled = True
        has_collected = True

        async def push_to_subscribers(self, *_args, **_kwargs):
            return delivery_contract.DeliveryResult(
                delivery_contract.DeliveryState.QUEUED
            )

        async def flush_collected(self, **_kwargs):
            successful = frozenset({"tester"}) if flush_succeeds else frozenset()
            failed = frozenset() if flush_succeeds else frozenset({"tester"})
            return delivery_contract.CollectiveFlushResult(successful, failed)

        def clear_collected(self):
            self.has_collected = False

    api = API()
    subscriptions = plugin_module.SubscriptionService(
        get_kv, put_kv, api, lambda: True
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    assert await polling.check_user("tester", store["tester"])
    assert store["tester"]["since_id"] == "100"
    assert polling.has_pending_collective is True

    await polling.flush_pending_collective()
    expected_cursor = "101" if flush_succeeds else "100"
    expected_processed = ["101"] if flush_succeeds else []
    assert store["tester"]["since_id"] == expected_cursor
    assert store["tester"].get("processed_tweet_ids", []) == expected_processed
    assert polling.has_pending_collective is False


@pytest.mark.asyncio
async def test_cursor_updates_are_monotonic(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {},
        }
    }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    subscriptions = plugin_module.SubscriptionService(
        get_kv, put_kv, object(), lambda: True
    )

    assert await subscriptions.update_cursor("tester", "102")
    assert await subscriptions.update_cursor("tester", "101")
    assert store["tester"]["since_id"] == "102"


@pytest.mark.asyncio
async def test_provider_switch_skips_already_processed_tweet_ids(plugin_module):
    """切换数据源后即使游标偏旧，也不应再次发送已处理的推文。"""
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "processed_tweet_ids": ["101", "102"],
            "subscribers": {"session": {"status": True}},
        }
    }
    delivery_contract = _delivery_contract(plugin_module)
    detail_calls = []
    delivered = []

    class FxTwitterAPI:
        async def get_user_timeline_items(self, _username, _since_id):
            return [
                {
                    "tweet_id": tweet_id,
                    "username": "tester",
                    "is_retweet": False,
                }
                for tweet_id in ("101", "102", "103")
            ]

        async def get_tweet(self, _username, tweet_id):
            detail_calls.append(tweet_id)
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": "tester",
            }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    class Delivery:
        collective_enabled = False

        async def push_to_subscribers(self, _username, tweet_info, cycle=None, **_kwargs):
            delivered.append(tweet_info["tweet_id"])
            return delivery_contract.DeliveryResult(
                delivery_contract.DeliveryState.DELIVERED
            )

    api = FxTwitterAPI()
    subscriptions = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        api,
        lambda: True,
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        Delivery(),
        plugin_module.PollingSettings(
            include_retweets=True,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    assert await polling.check_user("tester", copy.deepcopy(store["tester"]))
    assert detail_calls == ["103"]
    assert delivered == ["103"]
    assert store["tester"]["since_id"] == "103"
    assert store["tester"]["processed_tweet_ids"] == ["101", "102", "103"]


@pytest.mark.asyncio
async def test_processed_tweet_history_is_bounded_and_legacy_safe(plugin_module):
    store = {
        "tester": {
            "screen_name": "Tester",
            "since_id": "100",
            "subscribers": {},
        }
    }

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    subscriptions = plugin_module.SubscriptionService(
        get_kv,
        put_kv,
        object(),
        lambda: True,
    )

    assert subscriptions.processed_tweet_ids(store["tester"]) == {"100"}
    tweet_ids = [str(tweet_id) for tweet_id in range(1, 506)]
    assert await subscriptions.commit_processed_tweets(
        "tester",
        tweet_ids,
        "505",
    )
    assert store["tester"]["since_id"] == "505"
    assert len(store["tester"]["processed_tweet_ids"]) == 500
    assert store["tester"]["processed_tweet_ids"][0] == "6"
    assert store["tester"]["processed_tweet_ids"][-1] == "505"

@pytest.mark.asyncio
@pytest.mark.parametrize("use_node,collective", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("outcome", ["success", "cancel_before_save", "cancel_after_save", "failure"])
async def test_delivery_history_and_cursor_share_one_write(plugin_module, use_node, collective, outcome):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "processed_tweet_ids": ["100"],
        "subscribers": {"group": {"status": True}},
    }}}
    before = copy.deepcopy(store)
    entered, release = asyncio.Event(), asyncio.Event()
    writes, sends = [], []

    async def get_kv(key, default):
        return store.get(key, default)  # Also exercise getters returning live objects.

    async def put_kv(key, value):
        writes.append((key, copy.deepcopy(value)))
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise OSError("KV unavailable")
        store[key] = copy.deepcopy(value)
        if outcome == "cancel_after_save":
            raise asyncio.CancelledError

    async def send_message(_umo, _message):
        sends.append(True)
        return True

    async def timeline(_username, since_id):
        return [{"tweet_id": "101", "username": "tester"}] if since_id == "100" else []

    async def get_tweet(_username, tweet_id):
        return {"status": True, "tweet_id": tweet_id, "text": "body"}

    plugin = plugin_module.TwitterPlugin(types.SimpleNamespace(send_message=send_message), {
        "twitter_use_node": use_node, "twitter_collective_forward": collective,
        "twitter_deduplicate_retweets": False,
    })
    plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = get_tweet

    async def run_poll():
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    task = asyncio.create_task(run_poll())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert sends == [True]
        assert len(writes) == 1
        key, candidate = writes[0]
        assert key == "twitter_subs"
        author = candidate["tester"]
        assert author["since_id"] == "101"  # Old code first writes history with cursor 100.
        assert author["processed_tweet_ids"] == ["100", "101"]
        assert author["subscribers"]["group"]["recent_deliveries"][0]["tweet_id"] == "101"
        assert store == before  # No live cache mutation while the write is blocked.
        if outcome == "cancel_before_save":
            task.cancel()
        release.set()  # Exceptional cleanup may make a second, receipt-only write.
        if outcome.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure" and collective:
            with pytest.raises(OSError):
                await task
        else:
            await task
        assert len(writes) == (2 if outcome in {"failure", "cancel_before_save"} else 1)
        if outcome == "failure":
            assert store == before
        elif outcome == "cancel_before_save":
            recovered_author = store["twitter_subs"]["tester"]
            assert recovered_author["since_id"] == "100"
            assert recovered_author["processed_tweet_ids"] == ["100"]
            assert recovered_author["subscribers"]["group"]["pending_delivery_ids"] == ["101"]
            await run_poll()
            assert sends == [True]
            assert store["twitter_subs"]["tester"]["since_id"] == "101"
        else:
            assert store["twitter_subs"]["tester"] == author
            # A restarted poll sees the committed cursor and never sends it again.
            await run_poll()
            assert sends == [True]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def review_retry_plugin_factory(plugin_module):
    def make(store, send_message, *, transport="collective", put_kv=None, dedup=False,
             provider="nitter", max_tweets=5, include_retweets=True):
        async def get_kv(key, default):
            return copy.deepcopy(store.get(key, default))

        async def save(key, value):
            store[key] = copy.deepcopy(value)

        async def timeline(username, since_id):
            return [{"tweet_id": "101", "username": "original" if dedup else username,
                     "is_retweet": dedup, "retweeter_username": username}] if since_id == "100" else []

        async def get_tweet(username, tweet_id):
            return {"status": True, "tweet_id": tweet_id, "username": username, "text": "body"}

        plugin = plugin_module.TwitterPlugin(types.SimpleNamespace(send_message=send_message), {
            "twitter_data_provider": provider,
            "twitter_poll_max_tweets_per_user": max_tweets,
            "twitter_use_node": transport != "plain",
            "twitter_collective_forward": transport == "collective",
            "twitter_deduplicate_retweets": dedup,
            "twitter_include_retweets": include_retweets,
        })
        plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv or save
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = get_tweet
        return plugin
    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("is_retweet", [False, True])
async def test_failed_window_survives_sliding_timeline_and_provider_failure(
    review_retry_plugin_factory, transport, is_retweet
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    current_ids = ["101", "102"]
    timeline_calls = []
    detail_calls = []
    contexts = []
    recovered = False
    timeline_unavailable = False
    detail_unavailable = False
    delivered = {"good": [], "bad": []}
    original_author = "original" if is_retweet else "tester"

    def texts(chain):
        for part in chain:
            if isinstance(part, Nodes):
                for node in part.nodes:
                    yield from texts(node.content)
            elif isinstance(part, Plain):
                yield part.text

    async def send_message(umo, message):
        if umo == "bad" and not recovered:
            return False
        delivered[umo].extend(texts(message.chain))
        return True

    async def timeline(username, since_id):
        timeline_calls.append(since_id)
        if timeline_unavailable:
            raise RuntimeError("timeline boundary unavailable")
        return [{"tweet_id": tweet_id, "username": original_author,
                 "is_retweet": is_retweet, "retweeter_username": username,
                 "retweeter_screen_name": "Tester"}
                for tweet_id in current_ids if int(tweet_id) > int(since_id)]

    async def get_tweet(username, tweet_id):
        detail_calls.append((username, tweet_id))
        return {"status": not detail_unavailable, "tweet_id": tweet_id,
                "username": username, "text": "body"}

    async def build_chain(_username, tweet_info, *_args, **_kwargs):
        contexts.append((tweet_info["username"], tweet_info.get("retweet")))
        return [Plain(tweet_info["tweet_id"])]

    async def poll(provider="nitter"):
        plugin = review_retry_plugin_factory(store, send_message, transport=transport, provider=provider)
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = get_tweet
        plugin.message_service.build_message_chain = build_chain
        result = await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()
        return result

    assert await poll()
    first_delivered = list(delivered["good"])
    current_ids = ["201", "202"]  # The failed batch has fallen off the Nitter homepage.
    for _ in range(3):
        assert await poll()
        assert delivered["good"] == first_delivered
        assert store["twitter_subs"]["tester"]["since_id"] == "100"
    author = store["twitter_subs"]["tester"]
    assert [item["tweet_id"] for item in author["pending_tweet_items"]] == ["101", "102"]
    timeline_unavailable = True
    assert await poll("fxtwitter")  # A failed timeline request must not gate the known retry window.
    assert timeline_calls == ["100"]
    detail_unavailable = True
    assert not await poll("fxtwitter")
    assert store["twitter_subs"]["tester"]["since_id"] == "100"
    detail_unavailable = False
    recovered = True
    assert await poll("fxtwitter")
    assert delivered == {"good": ["101", "102"], "bad": ["101", "102"]}
    assert all(username == original_author for username, _tweet_id in detail_calls)
    assert all(username == original_author for username, _retweet in contexts)
    expected_retweet = {"retweeter_username": "tester", "retweeter_screen_name": "Tester"} if is_retweet else None
    assert all(retweet == expected_retweet for _username, retweet in contexts)
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "102"
    assert "pending_tweet_items" not in author
    assert all("pending_delivery_ids" not in config for config in author["subscribers"].values())
    assert not await poll("fxtwitter")  # No window remains, so a new timeline failure stays a failure.
    timeline_unavailable = False
    assert await poll("fxtwitter")
    assert delivered == {"good": ["101", "102", "201", "202"], "bad": ["101", "102", "201", "202"]}


@pytest.mark.asyncio
async def test_collective_retry_window_has_a_fixed_admission_limit(
    review_retry_plugin_factory
):
    limit = 500
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    delivered = []

    async def send_message(umo, _message):
        if umo == "bad":
            return False
        delivered.append(umo)
        return True

    async def timeline(username, since_id):
        return [{"tweet_id": str(tweet_id), "username": username}
                for tweet_id in range(101, 101 + limit + 1) if tweet_id > int(since_id)]

    plugin = review_retry_plugin_factory(store, send_message, max_tweets=limit + 1)
    plugin.twitter_api.get_user_timeline_items = timeline
    for _ in range(2):
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()
        author = store["twitter_subs"]["tester"]
        assert author["since_id"] == "100"
        assert len(author["subscribers"]["good"]["pending_delivery_ids"]) == limit
        assert len(author["pending_tweet_items"]) == limit
    assert delivered == ["good"]


@pytest.mark.asyncio
async def test_legacy_receipts_are_recovered_in_bounded_batches_before_new_items(review_retry_plugin_factory):
    old_ids = [str(tweet_id) for tweet_id in range(101, 602)]
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {
            "good": {"status": True, "pending_delivery_ids": old_ids}, "bad": {"status": True},
        },
    }}}
    sent = []
    current_ids = ["701"]

    async def send_message(umo, message):
        for part in message.chain:
            parts = [item for node in part.nodes for item in node.content] if isinstance(part, Nodes) else [part]
            sent.extend((umo, part.text) for part in parts if isinstance(part, Plain))
        return True

    async def timeline(username, _since_id):
        return [{"tweet_id": tweet_id, "username": username} for tweet_id in current_ids]

    async def poll():
        plugin = review_retry_plugin_factory(store, send_message, max_tweets=500)

        async def build_chain(_username, tweet_info, *_args, **_kwargs):
            return [Plain(tweet_info["tweet_id"])]

        plugin.message_service.build_message_chain = build_chain
        plugin.twitter_api.get_user_timeline_items = timeline
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    await poll()
    assert sent == [("bad", tweet_id) for tweet_id in old_ids[:500]]
    assert store["twitter_subs"]["tester"]["since_id"] == "600"
    assert store["twitter_subs"]["tester"]["subscribers"]["good"]["pending_delivery_ids"] == ["601"]
    await poll()
    assert sent == [("bad", tweet_id) for tweet_id in old_ids]
    assert store["twitter_subs"]["tester"]["since_id"] == "601"
    assert "pending_delivery_ids" not in store["twitter_subs"]["tester"]["subscribers"]["good"]
    await poll()
    assert sent[-2:] == [("good", "701"), ("bad", "701")]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
async def test_retry_window_orders_original_ids_before_cursor_cleanup(review_retry_plugin_factory, transport):
    # A later repost can have an older original ID than another timeline entry.
    items = [{"tweet_id": "102", "username": "tester", "is_retweet": False},
             {"tweet_id": "101", "username": "original", "is_retweet": True,
              "retweeter_username": "tester"}]
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "pending_tweet_items": items,
        "subscribers": {"good": {"status": True, "pending_delivery_ids": ["101", "102"]},
                        "bad": {"status": True}},
    }}}
    sent = []

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    async def get_tweet(username, tweet_id):
        return {"status": tweet_id != "101", "tweet_id": tweet_id, "username": username, "text": "body"}

    plugin = review_retry_plugin_factory(store, send_message, transport=transport)
    plugin.twitter_api.get_tweet = get_tweet
    assert not await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await plugin.polling_service.flush_pending_collective()
    assert sent == []
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "100"
    assert {item["tweet_id"] for item in author["pending_tweet_items"]} == {"101", "102"}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("remove_failed_session", [False, True])
async def test_config_skip_completes_acknowledged_window_without_tweet_details(
    review_retry_plugin_factory, transport, remove_failed_session
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    sent = []
    detail_calls = []
    current_id = "101"

    async def send_message(umo, _message):
        if umo == "bad":
            return False
        sent.append(umo)
        return True

    async def timeline(username, since_id):
        return [{"tweet_id": current_id, "username": username}] if int(current_id) > int(since_id) else []

    async def get_tweet(username, tweet_id):
        detail_calls.append(tweet_id)
        return {"status": tweet_id == current_id, "tweet_id": tweet_id, "username": username, "text": "body"}

    def make():
        plugin = review_retry_plugin_factory(store, send_message, transport=transport)
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = get_tweet
        return plugin

    async def poll(plugin):
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    plugin = make()
    await poll(plugin)
    assert sent == ["good"]
    if remove_failed_session:
        await plugin.subscription_service.remove("bad", "tester")
    else:
        await plugin.subscription_service.update("bad", "tester", {"enabled": False})
    current_id = "201"  # The old details are unavailable after the failed session is skipped.
    await poll(make())
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "101"
    assert "pending_tweet_items" not in author
    assert sent == ["good"]
    assert detail_calls == ["101"]
    await poll(make())
    assert sent == ["good", "good"]
    assert store["twitter_subs"]["tester"]["since_id"] == "201"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,cancel_at,tweet_ids", [
    (transport, cancel_at, ["101"])
    for transport in ("plain", "node", "collective")
    for cancel_at in ("next_session", "media")
] + [("collective", "media", ["101", "102"])])
async def test_cancelled_delivery_preserves_accepted_primary_messages(
    review_retry_plugin_factory, transport, cancel_at, tweet_ids
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    blocked = asyncio.Event()
    interrupt = True
    primary_deliveries = []

    async def send_message(umo, message):
        is_video = any(isinstance(part, Video) for part in message.chain)
        if interrupt and ((cancel_at == "next_session" and umo == "bad")
                          or (cancel_at == "media" and is_video)):
            blocked.set()
            await asyncio.Event().wait()
        if not is_video:
            primary_deliveries.append(umo)
        return True

    async def build_chain(*_args, **_kwargs):
        return [Plain("body"), Video.fromURL("https://example.com/video.mp4")]

    def reload_plugin():
        plugin = review_retry_plugin_factory(store, send_message, transport=transport)

        async def timeline(username, since_id):
            return [{"tweet_id": tweet_id, "username": username}
                    for tweet_id in tweet_ids if int(tweet_id) > int(since_id)]

        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.message_service.build_message_chain = build_chain
        return plugin

    async def poll(plugin):
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    plugin = reload_plugin()
    task = asyncio.create_task(poll(plugin))
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert primary_deliveries == ["good"]
        assert store["twitter_subs"]["tester"]["since_id"] == "100"
        assert store["twitter_subs"]["tester"]["subscribers"]["good"]["pending_delivery_ids"] == tweet_ids
        interrupt = False
        plugin = reload_plugin()
        await poll(plugin)
        assert primary_deliveries == ["good", "bad"]
        assert store["twitter_subs"]["tester"]["since_id"] == tweet_ids[-1]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_aborted_collective_flush_preserves_accepted_primary_messages(review_retry_plugin_factory):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    primary_deliveries = []

    async def send_message(umo, message):
        if not any(isinstance(part, Video) for part in message.chain):
            primary_deliveries.append(umo)
        return True

    async def build_chain(*_args, **_kwargs):
        return [Plain("body"), Video.fromURL("https://example.com/video.mp4")]

    async def failed_media(*_args):
        raise RuntimeError("unexpected delivery interruption")

    plugin = review_retry_plugin_factory(store, send_message)
    plugin.message_service.build_message_chain = build_chain
    plugin.delivery_service.send_video_or_fallback = failed_media
    await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await plugin.polling_service.flush_pending_collective()
    assert store["twitter_subs"]["tester"]["since_id"] == "100"
    plugin = review_retry_plugin_factory(store, send_message)
    plugin.message_service.build_message_chain = build_chain
    await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await plugin.polling_service.flush_pending_collective()
    assert primary_deliveries == ["good", "bad"]
    assert store["twitter_subs"]["tester"]["since_id"] == "101"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "cancel_before_save", "cancel_after_save"])
async def test_interrupted_collective_cursor_commit_preserves_all_uncommitted_authors(
    review_retry_plugin_factory, outcome
):
    store = {"twitter_subs": {
        "complete": {"since_id": "100", "subscribers": {"good": {"status": True}}},
        "later": {"since_id": "100", "subscribers": {"good": {"status": True}}},
        "partial": {"since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")}},
    }}
    fail_commit = True
    recovered = False
    delivered = []

    async def put_kv(key, value):
        nonlocal fail_commit
        if key == "twitter_subs" and value["complete"]["since_id"] == "101" and fail_commit:
            fail_commit = False
            if outcome == "failure":
                raise OSError("cursor KV unavailable")
            if outcome == "cancel_after_save":
                store[key] = copy.deepcopy(value)
            raise asyncio.CancelledError
        store[key] = copy.deepcopy(value)

    async def send_message(umo, message):
        if umo == "bad" and not recovered:
            return False
        for part in message.chain:
            contents = [item for node in part.nodes for item in node.content] if isinstance(part, Nodes) else [part]
            delivered.extend((umo, item.text) for item in contents if isinstance(item, Plain))
        return True

    async def build_chain(username, *_args, **_kwargs):
        return [Plain(username)]

    async def check(plugin):
        for username, author in copy.deepcopy(store["twitter_subs"]).items():
            await plugin.polling_service.check_user(username, author)

    plugin = review_retry_plugin_factory(store, send_message, put_kv=put_kv)
    plugin.message_service.build_message_chain = build_chain
    await check(plugin)
    with pytest.raises(OSError if outcome == "failure" else asyncio.CancelledError):
        await plugin.polling_service.flush_pending_collective()
    assert store["twitter_subs"]["later"]["subscribers"]["good"]["pending_delivery_ids"] == ["101"]
    plugin = review_retry_plugin_factory(store, send_message, put_kv=put_kv)
    plugin.message_service.build_message_chain = build_chain
    await check(plugin)
    await plugin.polling_service.flush_pending_collective()
    assert delivered.count(("good", "complete")) == 1
    assert delivered.count(("good", "later")) == 1
    assert delivered.count(("good", "partial")) == 1
    assert store["twitter_subs"]["partial"]["since_id"] == "100"
    recovered = True
    await check(plugin)
    await plugin.polling_service.flush_pending_collective()
    assert delivered.count(("good", "partial")) == 1
    assert delivered.count(("bad", "partial")) == 1
    assert store["twitter_subs"]["partial"]["since_id"] == "101"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("has_text", [True, False])
async def test_cancelled_image_fallback_preserves_accepted_primary_message(
    review_retry_plugin_factory, transport, has_text
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    blocked = asyncio.Event()
    interrupt = True
    delivered = []

    async def send_message(umo, message):
        if any(isinstance(part, Nodes) for part in message.chain) or len(message.chain) > 1:
            return False  # Exercise the ordinary fallback after the combined chain fails.
        part = message.chain[0]
        if isinstance(part, Plain) or part.file.endswith("first.jpg"):
            delivered.append(umo)
            return True
        if interrupt:
            blocked.set()
            await asyncio.Event().wait()
        return True

    async def build_chain(*_args, **_kwargs):
        primary = Plain("body") if has_text else Image.fromURL("https://example.com/first.jpg")
        return [primary, Image.fromURL("https://example.com/second.jpg")]

    async def poll():
        plugin = review_retry_plugin_factory(store, send_message, transport=transport)
        plugin.message_service.build_message_chain = build_chain
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    task = asyncio.create_task(poll())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert delivered == ["good"]
        assert store["twitter_subs"]["tester"]["since_id"] == "100"
        assert store["twitter_subs"]["tester"]["subscribers"]["good"]["pending_delivery_ids"] == ["101"]
        interrupt = False
        await poll()
        assert delivered == ["good", "bad"]
        assert store["twitter_subs"]["tester"]["since_id"] == "101"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("changes", [{"enabled": False}, {"r18": False}, {"media_only": True}])
async def test_receipts_restore_retweet_dedup_before_changed_filters(
    review_retry_plugin_factory, transport, changes
):
    store = {"twitter_subs": {username: {
        "since_id": "100", "subscribers": {"group": {"status": True, "r18": True}},
    } for username in ("tester", "other")}, "twitter_retweet_dedup_seen": {}}
    sent = []
    fail_seen_write = True

    async def put_kv(key, value):
        nonlocal fail_seen_write
        if key == "twitter_retweet_dedup_seen" and fail_seen_write:
            fail_seen_write = False
            raise OSError("dedup KV unavailable")
        store[key] = copy.deepcopy(value)

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    async def get_tweet(username, tweet_id):
        return {"status": True, "tweet_id": tweet_id, "username": username, "text": "body", "is_r18": True}

    def reload_plugin():
        plugin = review_retry_plugin_factory(store, send_message, transport=transport, put_kv=put_kv, dedup=True)
        plugin.twitter_api.get_tweet = get_tweet
        return plugin

    async def poll(plugin, username):
        await plugin.polling_service.check_user(username, copy.deepcopy(store["twitter_subs"][username]))
        await plugin.polling_service.flush_pending_collective()

    plugin = reload_plugin()
    await poll(plugin, "tester")
    assert store["twitter_retweet_dedup_seen"] == {}
    assert sent == ["group"]
    await plugin.subscription_service.update("group", "tester", changes)
    plugin = reload_plugin()
    await poll(plugin, "tester")
    assert store["twitter_retweet_dedup_seen"] == {"group": ["101"]}
    assert store["twitter_subs"]["tester"]["since_id"] == "101"
    await poll(plugin, "other")
    assert sent == ["group"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
async def test_interrupted_receipt_is_recoverable_when_later_window_write_fails(
    review_retry_plugin_factory, transport
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {
            "filtered": {"status": True, "media": True},
            **{umo: {"status": True} for umo in ("good", "bad")},
        },
    }}}
    writes = []
    interrupted = True
    sent = []

    async def put_kv(key, value):
        if key == "twitter_subs":
            if writes and interrupted:
                raise OSError("window write unavailable after receipt save")
            writes.append(copy.deepcopy(value))
        store[key] = copy.deepcopy(value)

    async def send_message(umo, _message):
        if umo == "bad" and interrupted:
            raise asyncio.CancelledError
        sent.append(umo)
        return True

    async def poll(plugin):
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    plugin = review_retry_plugin_factory(store, send_message, transport=transport, put_kv=put_kv, dedup=True)
    with pytest.raises(asyncio.CancelledError):
        await poll(plugin)
    saved = writes[0]["tester"]
    assert saved["subscribers"]["good"]["pending_delivery_ids"] == ["101"]
    assert saved["subscribers"]["filtered"]["pending_skip_ids"] == ["101"]
    assert "pending_delivery_ids" not in saved["subscribers"]["filtered"]
    assert saved["pending_tweet_items"] == [{
        "tweet_id": "101", "username": "original", "is_retweet": True,
        "retweeter_username": "tester", "retweeter_screen_name": "",
    }]
    assert saved["since_id"] == "100"
    interrupted = False
    plugin = review_retry_plugin_factory(store, send_message, transport=transport, dedup=True)

    async def unavailable_timeline(*_args):
        raise AssertionError("retry must not depend on the old item still being in the timeline")

    plugin.twitter_api.get_user_timeline_items = unavailable_timeline
    await poll(plugin)
    assert sent == ["good", "bad"]
    assert store["twitter_subs"]["tester"]["since_id"] == "101"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("skip_reason", ["media", "r18", "dedup"])
async def test_filtered_session_stays_completed_after_details_disappear(
    review_retry_plugin_factory, transport, skip_reason
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {
            "good": {"status": True, "r18": True},
            "filtered": {"status": True, "r18": skip_reason != "r18", "media": skip_reason == "media"},
            "bad": {"status": True, "r18": True},
        },
    }}, "twitter_retweet_dedup_seen": {"filtered": ["101"]} if skip_reason == "dedup" else {}}
    sent = []

    async def send_message(umo, _message):
        if umo == "bad":
            return False
        sent.append(umo)
        return True

    async def details(username, tweet_id):
        return {"status": True, "username": username, "tweet_id": tweet_id,
                "text": "body", "is_r18": skip_reason == "r18"}

    async def poll(plugin):
        await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()

    plugin = review_retry_plugin_factory(store, send_message, transport=transport, dedup=True)
    plugin.twitter_api.get_tweet = details
    await poll(plugin)
    assert sent == ["good"]
    await plugin.subscription_service.update("bad", "tester", {"enabled": False})
    # A later config change does not undo the explicit skip of this old item.
    await plugin.subscription_service.add("filtered", "tester", r18=True)
    store["twitter_retweet_dedup_seen"].pop("filtered", None)
    plugin = review_retry_plugin_factory(store, send_message, transport=transport, dedup=True)

    async def missing_details(*_args):
        return {"status": False}

    plugin.twitter_api.get_tweet = missing_details
    await poll(plugin)
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "101"
    assert "pending_tweet_items" not in author
    assert sent == ["good"]
    assert "filtered" not in store["twitter_retweet_dedup_seen"]
    assert not author["subscribers"]["filtered"].get("recent_deliveries")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("provider", ["nitter", "fxtwitter"])
async def test_orphan_receipt_recovers_directly_after_timeline_item_disappears(
    review_retry_plugin_factory, transport, provider
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {
            "good": {"status": True, "pending_delivery_ids": ["101"]},
            "bad": {"status": True},
        },
    }}}
    sent = []
    detail_calls = []
    detail_available = False

    async def send_message(umo, message):
        for part in message.chain:
            parts = [item for node in part.nodes for item in node.content] if isinstance(part, Nodes) else [part]
            sent.extend((umo, part.text) for part in parts if isinstance(part, Plain))
        return True

    async def timeline(username, since_id):
        return [{"tweet_id": "201", "username": username}] if int(since_id) < 201 else []

    async def details(username, tweet_id):
        detail_calls.append(tweet_id)
        return {"status": detail_available, "tweet_id": tweet_id,
                "username": "original" if tweet_id == "101" else username, "text": "body"}

    async def build_chain(_username, tweet_info, *_args, **_kwargs):
        if tweet_info["tweet_id"] == "101":
            assert tweet_info["username"] == "original"
            assert tweet_info["retweet"]["retweeter_username"] == "tester"
        return [Plain(tweet_info["tweet_id"])]

    async def poll():
        plugin = review_retry_plugin_factory(store, send_message, transport=transport, provider=provider, dedup=True)
        plugin.twitter_api.get_user_timeline_items = timeline
        plugin.twitter_api.get_tweet = details
        plugin.message_service.build_message_chain = build_chain
        result = await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
        await plugin.polling_service.flush_pending_collective()
        return result

    assert not await poll()
    assert sent == []
    assert store["twitter_subs"]["tester"]["since_id"] == "100"
    detail_available = True
    assert await poll()
    assert sent == [("bad", "101")]
    assert store["twitter_subs"]["tester"]["since_id"] == "101"
    assert await poll()
    assert sorted(sent) == [("bad", "101"), ("bad", "201"), ("good", "201")]
    assert detail_calls[:2] == ["101", "101"]


@pytest.mark.asyncio
async def test_interrupted_collective_saves_older_items_without_any_success_receipt(review_retry_plugin_factory):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {umo: {"status": True} for umo in ("good", "bad")},
    }}}
    interrupted = True
    writes = []
    sent = []

    async def put_kv(key, value):
        if key == "twitter_subs":
            if writes and interrupted:
                raise OSError("second write unavailable")
            writes.append(copy.deepcopy(value))
        store[key] = copy.deepcopy(value)

    async def timeline(username, _since_id):
        return [{"tweet_id": tweet_id, "username": username} for tweet_id in ("101", "102")]

    async def build_chain(_username, tweet_info, *_args, **_kwargs):
        if interrupted and tweet_info["tweet_id"] == "101":
            raise RuntimeError("oldest item cannot be built for any session")
        return [Plain(tweet_info["tweet_id"])]

    async def send_message(umo, _message):
        if interrupted and umo == "bad":
            raise asyncio.CancelledError
        sent.append(umo)
        return True

    plugin = review_retry_plugin_factory(store, send_message, put_kv=put_kv)
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.message_service.build_message_chain = build_chain
    await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    with pytest.raises(asyncio.CancelledError):
        await plugin.polling_service.flush_pending_collective()
    author = store["twitter_subs"]["tester"]
    assert author["subscribers"]["good"]["pending_delivery_ids"] == ["102"]
    assert [item["tweet_id"] for item in author["pending_tweet_items"]] == ["101", "102"]
    interrupted = False
    plugin = review_retry_plugin_factory(store, send_message)

    async def unavailable_oldest(username, tweet_id):
        return {"status": tweet_id != "101", "username": username, "tweet_id": tweet_id}

    plugin.twitter_api.get_tweet = unavailable_oldest
    assert not await plugin.polling_service.check_user("tester", copy.deepcopy(author))
    await plugin.polling_service.flush_pending_collective()
    assert store["twitter_subs"]["tester"]["since_id"] == "100"
    assert sent == ["good"]


@pytest.mark.asyncio
async def test_older_orphan_receipt_is_recovered_before_partial_window(review_retry_plugin_factory):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "pending_tweet_items": [{"tweet_id": "102", "username": "tester"}],
        "subscribers": {"good": {"status": True, "pending_delivery_ids": ["101", "102"]},
                        "bad": {"status": True}},
    }}}
    calls = []

    async def send_message(*_args):
        raise AssertionError("must not send past failed old orphan")

    async def details(_username, tweet_id):
        calls.append(tweet_id)
        return {"status": False}

    plugin = review_retry_plugin_factory(store, send_message)
    plugin.twitter_api.get_tweet = details
    assert not await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await plugin.polling_service.flush_pending_collective()
    assert calls == ["101"]
    assert store["twitter_subs"]["tester"]["since_id"] == "100"


@pytest.mark.asyncio
async def test_queued_retweet_is_not_a_persisted_skip_until_actual_send(review_retry_plugin_factory):
    store = {"twitter_subs": {username: {
        "since_id": "100", "subscribers": {"group": {"status": True}},
    } for username in ("first", "dependent")}}
    recovered = False
    sent = []

    async def send_message(umo, _message):
        if not recovered:
            return False
        sent.append(umo)
        return True

    async def poll():
        plugin = review_retry_plugin_factory(store, send_message, dedup=True)
        for username, author in copy.deepcopy(store["twitter_subs"]).items():
            await plugin.polling_service.check_user(username, author)
        await plugin.polling_service.flush_pending_collective()

    await poll()
    assert all(author["since_id"] == "100" for author in store["twitter_subs"].values())
    dependent = store["twitter_subs"]["dependent"]["subscribers"]["group"]
    assert not dependent.get("pending_skip_ids")
    assert not store.get("twitter_retweet_dedup_seen")
    recovered = True
    await poll()
    await poll()
    assert sent == ["group"]
    assert all(author["since_id"] == "101" for author in store["twitter_subs"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("timeline_available", [False, True])
async def test_orphan_self_retweet_uses_available_timeline_or_delivery_history(
    review_retry_plugin_factory, timeline_available
):
    store = {"twitter_subs": {"tester": {
        "since_id": "100", "subscribers": {
            "good": {"status": True, "pending_delivery_ids": ["101"],
                     "recent_deliveries": [] if timeline_available else [{"tweet_id": "101", "is_retweet": True}]},
            "bad": {"status": True},
        },
    }}}
    sent = []

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    async def timeline(username, _since_id):
        if not timeline_available:
            raise RuntimeError("timeline unavailable but details still work")
        return [{"tweet_id": "101", "username": username, "is_retweet": True,
                 "retweeter_username": username}]

    async def build_chain(_username, tweet_info, *_args, **_kwargs):
        assert tweet_info["retweet"]["retweeter_username"] == "tester"
        return [Plain("body")]

    plugin = review_retry_plugin_factory(store, send_message, dedup=True)
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.message_service.build_message_chain = build_chain
    await plugin.polling_service.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await plugin.polling_service.flush_pending_collective()
    assert sent == ["bad"]
    assert store["twitter_subs"]["tester"]["since_id"] == "101"
    assert store["twitter_retweet_dedup_seen"] == {"good": ["101"], "bad": ["101"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize(("dedup", "include_retweets"), [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("fail_restore", [False, True])
async def test_confirmed_retweet_restores_seen_across_global_config_changes(
    review_retry_plugin_factory, transport, dedup, include_retweets, fail_restore
):
    store = {"twitter_subs": {username: {
        "since_id": "100", "subscribers": {"group": {"status": True}},
    } for username in ("tester", "other")}, "twitter_retweet_dedup_seen": {}}
    sent = []
    failures = 1

    async def put_kv(key, value):
        nonlocal failures
        if key == "twitter_retweet_dedup_seen" and failures:
            failures -= 1
            raise OSError("retweet state unavailable")
        store[key] = copy.deepcopy(value)

    async def send_message(umo, _message):
        sent.append(umo)
        return True

    async def poll(username, *, dedup=True, include_retweets=True):
        plugin = review_retry_plugin_factory(
            store, send_message, transport=transport, put_kv=put_kv,
            dedup=dedup, include_retweets=include_retweets,
        )
        await plugin.polling_service.check_user(username, copy.deepcopy(store["twitter_subs"][username]))
        await plugin.polling_service.flush_pending_collective()

    await poll("tester")
    assert sent == ["group"]
    assert store["twitter_retweet_dedup_seen"] == {}
    if not include_retweets:
        # A newly added target is explicitly skipped, never marked as actually sent.
        store["twitter_subs"]["tester"]["subscribers"]["late"] = {"status": True}
    if fail_restore:
        failures = 1
        await poll("tester", dedup=dedup, include_retweets=include_retweets)
        author = store["twitter_subs"]["tester"]
        assert author["since_id"] == "100"
        assert author["subscribers"]["group"]["pending_delivery_ids"] == ["101"]
    await poll("tester", dedup=dedup, include_retweets=include_retweets)
    assert store["twitter_retweet_dedup_seen"] == {"group": ["101"]}
    assert store["twitter_subs"]["tester"]["since_id"] == "101"
    await poll("other")
    assert store["twitter_subs"]["other"]["since_id"] == "101"
    assert sent == ["group"]
