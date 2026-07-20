import asyncio
import copy
import importlib.util
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
    pass


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
        self.fx_checks = 0
        self.nitter_checks = 0
        self.closed = False
        self.__class__.instances.append(self)

    async def check_fxtwitter_available(self):
        self.fx_checks += 1
        return True

    async def check_website_available(self, websites):
        self.nitter_checks += 1
        self.nitter_url = websites[0] if websites else "https://nitter.test"
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


async def _wait_forever():
    await asyncio.Event().wait()


def _delivery_contract(plugin_module):
    return sys.modules[
        f"{plugin_module.__package__}.services.tweet_delivery_service"
    ]


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


def test_flat_and_grouped_provider_config_are_compatible(plugin_module):
    assert plugin_module.TwitterWebUIController is None

    flat = plugin_module.TwitterPlugin(
        object(),
        {
            "twitter_data_provider": "fxtwitter",
            "twitter_fxtwitter_api_base": "https://fx.example/",
            "twitter_poll_max_tweets_per_user": 7,
        },
    )
    grouped = plugin_module.TwitterPlugin(
        object(),
        {
            "basic": {
                "twitter_data_provider": "fxtwitter",
                "twitter_fxtwitter_api_base": "https://grouped.example/",
                "twitter_poll_max_tweets_per_user": 9,
            }
        },
    )

    assert flat.data_provider == "fxtwitter"
    assert flat.fxtwitter_api_base == "https://fx.example"
    assert flat.poll_max_tweets_per_user == 7
    assert grouped.data_provider == "fxtwitter"
    assert grouped.fxtwitter_api_base == "https://grouped.example"
    assert grouped.poll_max_tweets_per_user == 9

    defaulted = plugin_module.TwitterPlugin(object(), {})
    assert defaulted.poll_max_tweets_per_user == 5


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

    class Delivery:
        async def push_to_subscribers(self, *_args, **_kwargs):
            raise AssertionError("关闭转帖后不应进入发送流程")

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
            include_retweets=False,
            data_provider="fxtwitter",
            custom_nitter_url="",
            website_list=(),
        ),
    )

    assert await polling.check_user("tester", store["tester"]) is True
    assert store["tester"]["since_id"] == "101"


@pytest.mark.asyncio
async def test_test_command_and_link_recognition_share_prepared_delivery(
    plugin_module,
):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin._provider_ready = True
    plugin.include_retweets = True
    plugin.link_recognition_enabled = True

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
        async def maybe_translate(self, _tweet_info, _umo):
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

    assert test_results[-1][0].text == "prepared"
    assert link_results[0][0].text == "prepared"
    assert plugin.delivery_service.prepare_calls == 2


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

        async def push_to_subscribers(self, _username, tweet_info):
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

        async def flush_collected(self):
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

        async def push_to_subscribers(self, _username, tweet_info):
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
