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
    flat = plugin_module.TwitterPlugin(
        object(),
        {
            "twitter_data_provider": "fxtwitter",
            "twitter_fxtwitter_api_base": "https://fx.example/",
        },
    )
    grouped = plugin_module.TwitterPlugin(
        object(),
        {
            "basic": {
                "twitter_data_provider": "fxtwitter",
                "twitter_fxtwitter_api_base": "https://grouped.example/",
            }
        },
    )

    assert flat.data_provider == "fxtwitter"
    assert flat.fxtwitter_api_base == "https://fx.example"
    assert grouped.data_provider == "fxtwitter"
    assert grouped.fxtwitter_api_base == "https://grouped.example"


@pytest.mark.asyncio
async def test_detail_failure_only_advances_cursor_to_last_success(plugin_module):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.include_retweets = True
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

    async def get_subs():
        return store

    async def save_subs(data):
        saved = copy.deepcopy(data)
        store.clear()
        store.update(saved)

    async def push(*_args, **_kwargs):
        return None

    plugin.twitter_api = API()
    plugin._get_subs = get_subs
    plugin._save_subs = save_subs
    plugin._push_tweet_to_subscribers = push

    result = await plugin._check_user_tweets("tester", store["tester"])

    assert result is False
    assert store["tester"]["since_id"] == "101"
