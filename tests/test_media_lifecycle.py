import asyncio
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
        if not url.startswith(("http://", "https://")):
            raise ValueError("not a valid URL")
        return Image(file=url)

    @staticmethod
    def fromFileSystem(path):
        return Image(file=Path(path).resolve().as_uri(), path=str(Path(path).resolve()))


class Video(_Component):
    @staticmethod
    def fromURL(url):
        return Video(file=url)


class Node(_Component):
    def __init__(self, content, name):
        super().__init__(content=content, name=name)


class Nodes(_Component):
    def __init__(self, nodes):
        super().__init__(nodes=nodes)


class MessageChain(_Component):
    def __init__(self, chain):
        super().__init__(chain=chain)


def _decorator(*_args, **_kwargs):
    return lambda func: func


def _load_main_module():
    package_name = "twitter_plugin_test_package"
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
    twitter_api.TwitterAPI = object
    twitter_api.WEBSITE_LIST = []
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
    return _load_main_module()


@pytest.mark.asyncio
async def test_screenshot_uses_prepared_copy_but_sends_original_media(
    plugin_module, tmp_path
):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.no_text = False
    plugin.pre_download_media = True
    plugin.proxy = "http://127.0.0.1:7890"
    plugin.screenshot_theme = "dark"
    plugin.include_tweet_link = False

    original_url = "https://example.com/original.jpg"
    prepared_url = "data:image/jpeg;base64,cHJlcGFyZWQ="
    tweet_info = {
        "username": "tester",
        "tweet_id": "1",
        "images": [original_url],
    }
    captured_media = []
    captured_context = {}

    async def prepare_screenshot_media(_tweet_info):
        prepared = dict(_tweet_info)
        prepared["images"] = [prepared_url]
        return prepared

    async def html_render(_template, context, options):
        captured_context.update(context)
        assert options
        return str(tmp_path / "card.png")

    async def append_media(
        _chain, images, _videos, _temp_files, context_label="推文"
    ):
        captured_media.append((context_label, list(images)))

    plugin._prepare_screenshot_media = prepare_screenshot_media
    plugin.html_render = html_render
    plugin._append_media_components = append_media

    await plugin._build_screenshot_tweet_chain(
        "tester", tweet_info, [], {"r18": True, "media": False, "status": True}
    )

    assert captured_context["tweet"]["media"][0]["url"] == prepared_url
    assert captured_media == [("推文", [original_url])]
    assert tweet_info["images"] == [original_url]


@pytest.mark.asyncio
async def test_concurrent_sends_only_clean_their_own_files(plugin_module, tmp_path):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.use_node = False
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    paths = {"first": first_path, "second": second_path}

    second_started = asyncio.Event()
    release_second = asyncio.Event()

    class Context:
        async def send_message(self, umo, _message_chain):
            assert paths[umo].exists()
            if umo == "second":
                second_started.set()
                await release_second.wait()

    async def build_message(username, *_args, **_kwargs):
        path = paths[username]
        return plugin_module.BuiltTweetMessage(
            chain=[Image.fromFileSystem(path)],
            temp_files=[str(path)],
        )

    plugin.context = Context()
    plugin._build_tweet_message_chain = build_message

    second_task = asyncio.create_task(
        plugin._send_tweet_to_subscriber(
            "second", "second", {}, {}, "second"
        )
    )
    await second_started.wait()
    await plugin._send_tweet_to_subscriber("first", "first", {}, {}, "first")

    assert not first_path.exists()
    assert second_path.exists()

    release_second.set()
    await second_task
    assert not second_path.exists()


@pytest.mark.asyncio
async def test_collective_batch_cleans_files_after_success(plugin_module, tmp_path):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    media_path = tmp_path / "collective.jpg"
    media_path.write_bytes(b"collective")
    sent = False

    class Context:
        async def send_message(self, _umo, _message_chain):
            nonlocal sent
            assert media_path.exists()
            sent = True

    async def build_message(*_args, **_kwargs):
        return plugin_module.BuiltTweetMessage(
            chain=[Image.fromFileSystem(media_path)],
            temp_files=[str(media_path)],
        )

    plugin.context = Context()
    plugin._build_tweet_message_chain = build_message
    plugin._send_video_or_fallback = lambda *_args, **_kwargs: None
    cached_tweet = plugin_module.CachedTweet(
        username="tester",
        tweet_info={},
        sub_config={},
        nickname="tester",
    )

    await plugin._send_collected_batch(
        "umo", ["tester"], {"tester": [cached_tweet]}, 0, 1
    )

    assert sent
    assert not media_path.exists()
