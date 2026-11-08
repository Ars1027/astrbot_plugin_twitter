"""Function-scoped loaders and reusable plugin fixtures."""

import copy
import sys
import types

import pytest

from support.fakes import (FakeTwitterAPI, FakeContext, FakePlatform, Nodes, Image, Plain, telegram_platform)
from support.plugin_loader import load_main_module, load_api_module, load_webui_module


@pytest.fixture
def provider_plugin_module(monkeypatch):
    FakeTwitterAPI.instances.clear()
    return load_main_module(monkeypatch)


@pytest.fixture
def media_plugin_module(monkeypatch):
    return load_main_module(monkeypatch, media=True)


@pytest.fixture
def api_module(monkeypatch):
    return load_api_module(monkeypatch)


@pytest.fixture
def webui_module(monkeypatch):
    return load_webui_module(monkeypatch)


@pytest.fixture
def subscription_module(provider_plugin_module):
    return sys.modules[f"{provider_plugin_module.__package__}.services.subscription_service"]


@pytest.fixture
def plugin_env(provider_plugin_module):
    module = provider_plugin_module
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


@pytest.fixture
def albums(plugin_env, monkeypatch, tmp_path):
    module, make, data, ordinary, _fail = plugin_env
    sent = []
    converted = []

    class InputMediaPhoto:
        def __init__(self, *, media, caption, parse_mode):
            self.media = media
            self.caption = caption
            self.parse_mode = parse_mode

    monkeypatch.setitem(sys.modules, "telegram", types.SimpleNamespace(InputMediaPhoto=InputMediaPhoto))

    async def convert(image):
        converted.append(image)
        path = tmp_path / f"{len(converted)}.png"
        path.write_bytes(getattr(image, "data", b"image"))
        return str(path)

    monkeypatch.setattr(Image, "convert_to_file_path", convert, raising=False)

    async def send_media_group(**payload):
        sent.append(("album", payload))
        return tuple(object() for _ in payload["media"])

    async def send_photo(**payload):
        sent.append(("photo", payload))
        return object()

    plugin = make()
    client = plugin.context.platform_manager.platform_insts[0].client
    client.send_media_group = send_media_group
    client.send_photo = send_photo
    message_module = sys.modules[f"{module.__package__}.services.tweet_message_service"]

    def chain(images, *, body="", required=True, link="https://x.com/tester/status/101"):
        return message_module.TweetMessageChain(
            [*([Plain(body)] if body else []), *images, *([Plain(link)] if link else [])],
            required_images=images if required else (),
        )

    return plugin, data, ordinary, sent, converted, client, chain
