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
    twitter_api.DATA_PROVIDER_NITTER = "nitter"
    twitter_api.DATA_PROVIDER_FXTWITTER = "fxtwitter"
    twitter_api.DATA_PROVIDER_OPTIONS = ("nitter", "fxtwitter")
    twitter_api.DEFAULT_FXTWITTER_API_BASE = "https://api.fxtwitter.com"
    twitter_api.FxTwitterTimelineError = RuntimeError
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


def _delivery_contract(plugin_module):
    return sys.modules[
        f"{plugin_module.__package__}.services.tweet_delivery_service"
    ]


def _message_settings(plugin_module, **overrides):
    values = {
        "no_text": False,
        "send_media_separately": True,
        "include_tweet_link": False,
        "text_render_mode": "screenshot",
        "screenshot_theme": "dark",
        "video_max_size_mb": 256,
        "translate_enabled": False,
        "translate_target_lang": "简体中文",
        "translate_provider_id": "",
        "translate_custom_prompt_enabled": False,
        "translate_custom_prompt": "",
        "pre_download_media": True,
        "proxy": "http://127.0.0.1:7890",
    }
    values.update(overrides)
    return plugin_module.TweetMessageSettings(**values)


def _delivery_settings(plugin_module, **overrides):
    values = {
        "use_node": False,
        "collective_forward": False,
        "collective_max_authors": 5,
        "deduplicate_retweets": False,
    }
    values.update(overrides)
    return plugin_module.TweetDeliverySettings(**values)


@pytest.mark.asyncio
async def test_screenshot_uses_prepared_copy_but_sends_original_media(
    plugin_module, tmp_path
):
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

    service = plugin_module.TweetMessageService(
        object(),
        object(),
        html_render,
        _message_settings(plugin_module),
    )
    service.prepare_screenshot_media = prepare_screenshot_media
    service.append_media_components = append_media

    await service.build_screenshot_tweet_chain(
        "tester", tweet_info, {"r18": True, "media": False, "status": True}
    )

    assert captured_context["tweet"]["media"][0]["url"] == prepared_url
    assert captured_media == [("推文", [original_url])]
    assert tweet_info["images"] == [original_url]


@pytest.mark.asyncio
async def test_pre_downloaded_image_uses_from_bytes(plugin_module):
    image_bytes = b"downloaded-image"

    class TwitterAPI:
        async def download_media(self, url):
            assert url == "https://example.com/image.jpg"
            return image_bytes

    service = plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(plugin_module),
    )
    component = await service.build_image_component(
        "https://example.com/image.jpg"
    )

    assert isinstance(component, Image)
    assert component.data == image_bytes


@pytest.mark.asyncio
async def test_pre_download_failure_falls_back_to_remote_url(plugin_module):
    class TwitterAPI:
        async def download_media(self, _url):
            raise RuntimeError("proxy unavailable")

    service = plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(plugin_module),
    )
    image_url = "https://example.com/image.jpg"

    component = await service.build_image_component(image_url)

    assert isinstance(component, Image)
    assert component.file == image_url


@pytest.mark.asyncio
async def test_media_send_failure_preserves_text(plugin_module):
    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, _umo, message_chain):
            self.sent.append(message_chain.chain)
            if any(isinstance(component, Image) for component in message_chain.chain):
                raise RuntimeError("media unavailable")

    context = Context()
    delivery = plugin_module.TweetDeliveryService(
        context,
        object(),
        object(),
        _delivery_settings(plugin_module),
    )
    chain = [Plain("tweet text"), Image.fromURL("https://example.com/image.jpg")]

    sent = await delivery.send_plain_chain_resilient("session", chain)

    assert sent is True
    assert len(context.sent) == 3
    assert context.sent[1][0].text == "tweet text"


@pytest.mark.asyncio
async def test_primary_and_video_fallback_results_are_reported(plugin_module):
    class FailedContext:
        async def send_message(self, _umo, _message_chain):
            raise RuntimeError("adapter unavailable")

    failed_delivery = plugin_module.TweetDeliveryService(
        FailedContext(),
        object(),
        object(),
        _delivery_settings(plugin_module),
    )
    assert not await failed_delivery.send_plain_chain_resilient(
        "session", [Plain("tweet")]
    )

    class VideoFallbackContext:
        def __init__(self):
            self.sent = []

        async def send_message(self, _umo, message_chain):
            self.sent.append(message_chain.chain)
            if isinstance(message_chain.chain[0], Video):
                raise RuntimeError("video unavailable")

    context = VideoFallbackContext()
    delivery = plugin_module.TweetDeliveryService(
        context,
        object(),
        object(),
        _delivery_settings(plugin_module),
    )
    sent = await delivery.send_video_or_fallback(
        "session", Video.fromURL("https://example.com/video.mp4")
    )

    assert sent is True
    assert len(context.sent) == 2
    assert context.sent[1][0].text.startswith("视频: ")


@pytest.mark.asyncio
async def test_partial_subscriber_failure_is_reported(plugin_module):
    subscriptions_data = {
        "tester": {
            "screen_name": "Tester",
            "subscribers": {
                "good": {"status": True, "r18": True, "media": False},
                "bad": {"status": True, "r18": True, "media": False},
            },
        }
    }

    class Subscriptions:
        async def get_all(self):
            return subscriptions_data

        async def get_retweet_seen(self):
            return {}

        async def save_retweet_seen(self, _data):
            return None

    class Messages:
        @staticmethod
        def build_nickname(username, screen_name):
            return f"@{username} ({screen_name})"

        build_author_display = build_nickname

        @staticmethod
        def tweet_has_media(_tweet_info):
            return False

        async def maybe_translate(self, _tweet_info, _umo):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("tweet")]

    class Context:
        async def send_message(self, umo, _message_chain):
            if umo == "bad":
                raise RuntimeError("send failed")

    delivery = plugin_module.TweetDeliveryService(
        Context(),
        Subscriptions(),
        Messages(),
        _delivery_settings(plugin_module),
    )
    result = await delivery.push_to_subscribers(
        "tester",
        {
            "tweet_id": "1",
            "username": "tester",
            "screen_name": "Tester",
            "text": "tweet",
        },
    )

    contract = _delivery_contract(plugin_module)
    assert result.state is contract.DeliveryState.FAILED


@pytest.mark.asyncio
async def test_text_layout_keeps_retweet_quote_and_translation_paragraphs(
    plugin_module,
):
    service = plugin_module.TweetMessageService(
        object(),
        object(),
        None,
        _message_settings(
            plugin_module,
            text_render_mode="text",
            include_tweet_link=True,
            pre_download_media=False,
            proxy=None,
        ),
    )
    tweet_info = {
        "username": "author",
        "screen_name": "Author",
        "tweet_id": "123",
        "text": "original",
        "retweet": {
            "retweeter_username": "retweeter",
            "retweeter_screen_name": "Retweeter",
        },
        "quote": {
            "username": "quoted",
            "author": "Quoted",
            "text": "quoted original",
            "translated_text": "引用译文",
        },
    }

    chain = await service.build_message_chain(
        "retweeter",
        tweet_info,
        translated_text="正文译文",
        translate_model="model",
    )

    assert len(chain) == 1
    assert chain[0].text == (
        "@retweeter (Retweeter) 转发了 @author (Author) 的帖子\n\n"
        "正文译文\n\n"
        "@author (Author) 引用了 @quoted (Quoted) 的帖子\n\n"
        "引用译文\n\n"
        "https://x.com/author/status/123\n\n"
        "（由 model 翻译自原文）"
    )


@pytest.mark.asyncio
async def test_screenshot_failure_falls_back_to_text(plugin_module):
    async def html_render(*_args, **_kwargs):
        raise RuntimeError("render unavailable")

    service = plugin_module.TweetMessageService(
        object(),
        object(),
        html_render,
        _message_settings(
            plugin_module,
            text_render_mode="screenshot",
            pre_download_media=False,
            proxy=None,
        ),
    )

    chain = await service.build_message_chain(
        "tester",
        {
            "username": "tester",
            "screen_name": "Tester",
            "tweet_id": "1",
            "text": "fallback text",
        },
    )

    assert len(chain) == 1
    assert isinstance(chain[0], Plain)
    assert "fallback text" in chain[0].text


@pytest.mark.asyncio
async def test_disabling_separate_media_skips_all_media_work(plugin_module):
    class TwitterAPI:
        async def get_remote_file_size(self, _url):
            raise AssertionError("关闭媒体发送后不应探测视频大小")

        async def download_media(self, _url):
            raise AssertionError("关闭媒体发送后不应下载图片")

    service = plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(
            plugin_module,
            text_render_mode="text",
            send_media_separately=False,
        ),
    )

    chain = await service.build_message_chain(
        "tester",
        {
            "username": "tester",
            "tweet_id": "1",
            "text": "text",
            "images": ["https://example.com/image.jpg"],
            "videos": ["https://example.com/video.mp4"],
            "quote": {
                "username": "quoted",
                "text": "quote",
                "images": ["https://example.com/quote.jpg"],
                "videos": ["https://example.com/quote.mp4"],
            },
        },
    )

    assert len(chain) == 1
    assert isinstance(chain[0], Plain)


def test_prepared_delivery_is_shared_by_plain_and_node_modes(plugin_module):
    chain = [
        Plain("tweet text"),
        Image.fromURL("https://example.com/image.jpg"),
        Video.fromURL("https://example.com/video.mp4"),
    ]

    plain_delivery = plugin_module.TweetDeliveryService(
        object(),
        object(),
        object(),
        _delivery_settings(plugin_module),
    )
    plain = plain_delivery.prepare_event_delivery(chain, "Tester")

    node_delivery = plugin_module.TweetDeliveryService(
        object(),
        object(),
        object(),
        _delivery_settings(plugin_module, use_node=True),
    )
    node = node_delivery.prepare_event_delivery(chain, "Tester")

    assert [type(component) for component in plain.primary_chain] == [Plain, Image]
    assert len(plain.videos) == 1
    assert len(node.primary_chain) == 1
    assert isinstance(node.primary_chain[0], Nodes)
    assert len(node.primary_chain[0].nodes) == 2
    assert len(node.videos) == 1


@pytest.mark.asyncio
async def test_collective_delivery_flushes_and_clears_cache(plugin_module):
    subscriptions_data = {
        "tester": {
            "screen_name": "Tester",
            "subscribers": {
                "session": {"status": True, "r18": True, "media": False}
            },
        }
    }

    class Subscriptions:
        async def get_all(self):
            return subscriptions_data

        async def get_retweet_seen(self):
            return {}

        async def save_retweet_seen(self, _data):
            return None

    class Messages:
        @staticmethod
        def build_nickname(username, screen_name):
            return f"@{username} ({screen_name})"

        @staticmethod
        def build_author_display(username, screen_name):
            return f"@{username} ({screen_name})"

        @staticmethod
        def tweet_has_media(_tweet_info):
            return False

        async def maybe_translate(self, _tweet_info, _umo):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("tweet")]

    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, _umo, message_chain):
            self.sent.append(message_chain.chain)

    context = Context()
    delivery = plugin_module.TweetDeliveryService(
        context,
        Subscriptions(),
        Messages(),
        _delivery_settings(
            plugin_module,
            use_node=True,
            collective_forward=True,
        ),
    )

    queued = await delivery.push_to_subscribers(
        "tester",
        {
            "tweet_id": "1",
            "username": "tester",
            "screen_name": "Tester",
            "text": "tweet",
        },
    )

    contract = _delivery_contract(plugin_module)
    assert queued.state is contract.DeliveryState.QUEUED
    assert delivery.has_collected is True
    assert context.sent == []

    flushed = await delivery.flush_collected()

    assert delivery.has_collected is False
    assert len(context.sent) == 1
    assert isinstance(context.sent[0][0], Nodes)
    assert flushed.successful_authors == frozenset({"tester"})
    assert flushed.failed_authors == frozenset()


@pytest.mark.asyncio
async def test_failed_collective_retweet_does_not_persist_dedup(plugin_module):
    subscriptions_data = {
        "tester": {
            "screen_name": "Tester",
            "subscribers": {
                "session": {"status": True, "r18": True, "media": False}
            },
        }
    }
    saved_seen = []

    class Subscriptions:
        async def get_all(self):
            return subscriptions_data

        async def get_retweet_seen(self):
            return {}

        async def save_retweet_seen(self, data):
            saved_seen.append(data)

        @staticmethod
        def retweet_seen_by_umo(seen_data, umo, tweet_id):
            return plugin_module.SubscriptionService.retweet_seen_by_umo(
                seen_data, umo, tweet_id
            )

        @staticmethod
        def mark_retweet_seen(seen_data, umo, tweet_id):
            plugin_module.SubscriptionService.mark_retweet_seen(
                seen_data, umo, tweet_id
            )

    class Messages:
        @staticmethod
        def build_nickname(username, screen_name):
            return f"@{username} ({screen_name})"

        build_author_display = build_nickname

        @staticmethod
        def tweet_has_media(_tweet_info):
            return False

        async def maybe_translate(self, _tweet_info, _umo):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("retweet")]

    class Context:
        async def send_message(self, _umo, _message_chain):
            raise RuntimeError("adapter unavailable")

    delivery = plugin_module.TweetDeliveryService(
        Context(),
        Subscriptions(),
        Messages(),
        _delivery_settings(
            plugin_module,
            use_node=True,
            collective_forward=True,
            deduplicate_retweets=True,
        ),
    )
    queued = await delivery.push_to_subscribers(
        "tester",
        {
            "tweet_id": "123",
            "username": "original",
            "screen_name": "Original",
            "text": "retweet",
            "retweet": {
                "retweeter_username": "tester",
                "retweeter_screen_name": "Tester",
            },
        },
    )

    contract = _delivery_contract(plugin_module)
    assert queued.state is contract.DeliveryState.QUEUED
    assert saved_seen == []

    flushed = await delivery.flush_collected()

    assert flushed.successful_authors == frozenset()
    assert flushed.failed_authors == frozenset({"tester"})
    assert saved_seen == []
