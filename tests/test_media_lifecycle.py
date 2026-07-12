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

    @staticmethod
    def fromBytes(data):
        return Image(data=data)


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

    async def append_media(_chain, images, _videos, context_label="推文"):
        captured_media.append((context_label, list(images)))

    plugin._prepare_screenshot_media = prepare_screenshot_media
    plugin.html_render = html_render
    plugin._append_media_components = append_media

    await plugin._build_screenshot_tweet_chain(
        "tester", tweet_info, {"r18": True, "media": False, "status": True}
    )

    assert captured_context["tweet"]["media"][0]["url"] == prepared_url
    assert captured_media == [("推文", [original_url])]
    assert tweet_info["images"] == [original_url]


@pytest.mark.asyncio
async def test_pre_downloaded_image_uses_from_bytes(plugin_module):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.pre_download_media = True
    plugin.proxy = "http://127.0.0.1:7890"
    image_bytes = b"downloaded-image"

    class TwitterAPI:
        async def download_media(self, url):
            assert url == "https://example.com/image.jpg"
            return image_bytes

    plugin.twitter_api = TwitterAPI()

    component = await plugin._build_image_component(
        "https://example.com/image.jpg"
    )

    assert isinstance(component, Image)
    assert component.data == image_bytes


@pytest.mark.asyncio
async def test_pre_download_failure_falls_back_to_remote_url(plugin_module):
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin.pre_download_media = True
    plugin.proxy = "http://127.0.0.1:7890"

    class TwitterAPI:
        async def download_media(self, _url):
            raise RuntimeError("proxy unavailable")

    plugin.twitter_api = TwitterAPI()
    image_url = "https://example.com/image.jpg"

    component = await plugin._build_image_component(image_url)

    assert isinstance(component, Image)
    assert component.file == image_url
