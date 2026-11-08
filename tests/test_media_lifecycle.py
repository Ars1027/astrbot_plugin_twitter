import asyncio
import sys
import types

import pytest

from support.fakes import Plain, MediaImage as Image, Video, Nodes, _delivery_contract, _message_settings, _delivery_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "screenshot"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_both_message_modes_keep_main_media_before_quote(media_plugin_module, mode, enabled):
    async def render(*_args, **_kwargs):
        return "https://example.com/card.png"

    class API:
        async def get_remote_file_size(self, _url):
            return 1

    service = media_plugin_module.TweetMessageService(
        object(), API(), render, _message_settings(
            media_plugin_module, text_render_mode=mode, send_media_separately=enabled,
            pre_download_media=False,
        ),
    )
    chain = await service.build_message_chain("tester", {
        "text": "正文", "images": ["https://example.com/main.jpg"],
        "videos": ["https://example.com/main.mp4"],
        "quote": {"text": "引用", "images": ["https://example.com/quote.jpg"],
                  "videos": ["https://example.com/quote.mp4"]},
    })
    media = [component.file for component in chain if isinstance(component, (Image, Video))]
    expected = ["https://example.com/card.png"] if mode == "screenshot" else []
    if enabled:
        expected += ["https://example.com/main.jpg", "https://example.com/main.mp4",
                     "https://example.com/quote.jpg", "https://example.com/quote.mp4"]
    assert media == expected
    if mode == "text" and enabled:
        assert [type(component) for component in chain] == [Plain, Image, Video, Plain, Image, Video]
        assert "正文" in chain[0].text and "引用" not in chain[0].text
        assert "引用" in chain[3].text
        nodes, videos = media_plugin_module.TweetDeliveryService.split_chain_for_nodes(chain, "Tester")
        assert len(nodes) == 1
        assert [type(component) for component in nodes[0].content] == [Plain, Image, Plain, Image]
        assert [video.file for video in videos] == [
            "https://example.com/main.mp4", "https://example.com/quote.mp4",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_download, proxy", [(False, "http://proxy"), (True, ""), (True, "http://proxy")])
async def test_screenshot_pre_download_handles_both_parts_without_mutating_source(media_plugin_module, pre_download, proxy):
    import copy

    calls = []

    class API:
        async def download_media_to_data_uri(self, url):
            calls.append(url)
            if "failed" in url:
                raise RuntimeError("download failed")
            return "data:" + url

    service = media_plugin_module.TweetMessageService(
        object(), API(), None, _message_settings(media_plugin_module, pre_download_media=pre_download, proxy=proxy),
    )
    part = {"images": ["https://example.com/image", "https://example.com/failed"],
            "video_previews": [{"poster": "https://example.com/poster"}, {"poster": "https://example.com/failed"}],
            "article": {"cover": "https://example.com/cover", "blocks": []}}
    source = {**copy.deepcopy(part), "quote": copy.deepcopy(part)}
    original = copy.deepcopy(source)
    result = await service.prepare_screenshot_media(source)
    assert source == original
    for prepared in (result, result["quote"]):
        prefix = "data:" if pre_download and proxy else ""
        assert prepared["images"] == [prefix + "https://example.com/image", "https://example.com/failed"]
        assert prepared["video_previews"][0]["poster"] == prefix + "https://example.com/poster"
        assert prepared["video_previews"][1]["poster"] == "https://example.com/failed"
        assert prepared["article"]["cover"] == prefix + "https://example.com/cover"
    assert bool(calls) == bool(pre_download and proxy)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
@pytest.mark.parametrize("mode", ["success", "failure", "fallback", "empty", "history_error"])
async def test_recent_history_records_only_actual_session_success(
    media_plugin_module, transport, mode
):
    collective = transport == "collective"
    history = []
    subscriptions_data = {"tester": {"subscribers": {
        "good": {"status": True}, "bad": {"status": True},
        "paused": {"status": False},
    }}}

    class Subscriptions:
        async def get_all(self):
            return subscriptions_data

        async def get_retweet_seen(self):
            return {}

        async def record_delivery(self, umo, username, tweet_info):
            if mode == "history_error":
                raise OSError("history unavailable")
            history.append((umo, username, tweet_info["tweet_id"]))

    class Messages:
        build_nickname = staticmethod(lambda username, _name: username)

        async def maybe_translate(self, *_args, **_kwargs):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [] if mode == "empty" else await message_service.build_message_chain(
                "tester", {"text": "tweet", "images": ["https://example.com/main.jpg"]},
            )

    message_service = media_plugin_module.TweetMessageService(
        object(), object(), None, _message_settings(
            media_plugin_module, text_render_mode="text", pre_download_media=False,
        ),
    )

    async def send(umo, message):
        if isinstance(message.chain[0], Nodes):
            assert len(message.chain[0].nodes) == 1
            assert [type(part) for part in message.chain[0].nodes[0].content] == [Plain, Image]
        if umo == "bad" or mode == "failure":
            return False
        if mode == "fallback" and isinstance(message.chain[0], Nodes):
            return False
        return True

    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send), Subscriptions(), Messages(),
        _delivery_settings(media_plugin_module, use_node=transport != "plain", collective_forward=collective),
    )
    result = await delivery.push_to_subscribers("tester", {"tweet_id": "123", "text": "tweet"})
    contract = _delivery_contract(media_plugin_module)
    if collective:
        assert result.state is contract.DeliveryState.QUEUED
        assert history == []  # Merely queued is not a delivery.
        result = await delivery.flush_collected()
    assert history == []  # Polling persists summaries with its cursor, not during send.
    assert [(item.umo, item.username, item.record["tweet_id"]) for item in result.recent_deliveries] == (
        [] if mode in {"failure", "empty"} else [("good", "tester", "123")]
    )
    # A recording failure must not turn a successful send into a failed send.
    if mode == "history_error":
        assert await delivery.send_to_subscriber("good", "tester", {}, {}, "Tester")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "image", "node", "video"])
@pytest.mark.parametrize("outcome", [False, True, None, "fallback"])
async def test_send_return_values_and_fallback(media_plugin_module, mode, outcome):
    calls = []

    async def send_message(_umo, message):
        calls.append(message.chain)
        if outcome == "fallback":
            return len(calls) > 1
        return outcome

    chain = (
        [Image.fromURL("https://example.com/image.jpg")]
        if mode == "image"
        else [Plain("body")]
    )

    async def build_message_chain(*_args, **_kwargs):
        return chain

    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send_message),
        object(),
        types.SimpleNamespace(build_message_chain=build_message_chain),
        _delivery_settings(media_plugin_module, use_node=mode == "node"),
    )
    if mode == "video":
        sent = await delivery.send_video_or_fallback(
            "session", Video.fromURL("https://example.com/video.mp4")
        )
    else:
        sent = await delivery.send_to_subscriber("session", "u", {}, {}, "u")

    assert sent is (outcome is not False)
    if outcome is False or outcome == "fallback":
        assert len(calls) >= 2
        expected_type = Image if mode == "image" else Plain
        assert isinstance(calls[-1][0], expected_type)
    else:
        assert len(calls) == 1
    if mode == "node":
        assert isinstance(calls[0][0], Nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_succeeds", [False, True])
async def test_false_media_result_preserves_primary_semantics(
    media_plugin_module, body_succeeds
):
    async def send_message(_umo, message):
        is_body = any(
            isinstance(part, Plain) and part.text == "body"
            for part in message.chain
        )
        return body_succeeds if is_body else not body_succeeds

    async def build_message_chain(*_args, **_kwargs):
        return [Plain("body"), Video.fromURL("https://example.com/video.mp4")]

    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send_message),
        object(),
        types.SimpleNamespace(build_message_chain=build_message_chain),
        _delivery_settings(media_plugin_module),
    )
    assert await delivery.send_to_subscriber(
        "session", "u", {}, {}, "u"
    ) is body_succeeds


@pytest.mark.asyncio
@pytest.mark.parametrize("use_node", [False, True])
async def test_send_cancellation_propagates(media_plugin_module, use_node):
    async def send_message(*_args):
        raise asyncio.CancelledError

    async def build_message_chain(*_args, **_kwargs):
        return [Plain("body")]

    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send_message),
        object(),
        types.SimpleNamespace(build_message_chain=build_message_chain),
        _delivery_settings(media_plugin_module, use_node=use_node),
    )
    with pytest.raises(asyncio.CancelledError):
        await delivery.send_to_subscriber("session", "u", {}, {}, "u")


@pytest.mark.asyncio
async def test_translate_provider_selection_keeps_fallback_order(media_plugin_module):
    class Provider:
        def __init__(self, provider_id):
            self.provider_id = provider_id

        def meta(self):
            return types.SimpleNamespace(id=self.provider_id)

    class Context:
        def __init__(self, providers, current_provider_id=None):
            self.providers = providers
            self.current_provider_id = current_provider_id

        def get_provider_by_id(self, provider_id):
            return self.providers.get(provider_id)

        async def get_current_chat_provider_id(self, umo):
            assert umo == "session"
            return self.current_provider_id

        def get_all_providers(self):
            return list(self.providers.values())

    providers = {
        "configured": Provider("configured"),
        "current": Provider("current"),
    }
    configured_service = media_plugin_module.TweetMessageService(
        Context(providers, "current"),
        object(),
        None,
        _message_settings(
            media_plugin_module,
            translate_provider_id="configured",
        ),
    )
    current_service = media_plugin_module.TweetMessageService(
        Context(providers, "current"),
        object(),
        None,
        _message_settings(
            media_plugin_module,
            translate_provider_id="missing",
        ),
    )
    first_service = media_plugin_module.TweetMessageService(
        Context(providers),
        object(),
        None,
        _message_settings(media_plugin_module),
    )

    assert (
        await configured_service.get_translate_provider_id("session")
        == "configured"
    )
    assert (
        await current_service.get_translate_provider_id("session")
        == "current"
    )
    assert (
        await first_service.get_translate_provider_id("session")
        == "configured"
    )


@pytest.mark.asyncio
async def test_screenshot_uses_prepared_copy_but_sends_original_media(
    media_plugin_module, tmp_path
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

    async def append_media(
        _chain, images, _videos, context_label="推文", *, video_max_size_mb=None,
    ):
        assert video_max_size_mb is None
        captured_media.append((context_label, list(images)))

    service = media_plugin_module.TweetMessageService(
        object(),
        object(),
        html_render,
        _message_settings(media_plugin_module),
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
async def test_pre_downloaded_image_uses_from_bytes(media_plugin_module):
    image_bytes = b"downloaded-image"

    class TwitterAPI:
        async def download_media(self, url):
            assert url == "https://example.com/image.jpg"
            return image_bytes

    service = media_plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(media_plugin_module),
    )
    component = await service.build_image_component(
        "https://example.com/image.jpg"
    )

    assert isinstance(component, Image)
    assert component.data == image_bytes


@pytest.mark.asyncio
async def test_pre_download_failure_falls_back_to_remote_url(media_plugin_module):
    class TwitterAPI:
        async def download_media(self, _url):
            raise RuntimeError("proxy unavailable")

    service = media_plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(media_plugin_module),
    )
    image_url = "https://example.com/image.jpg"

    component = await service.build_image_component(image_url)

    assert isinstance(component, Image)
    assert component.file == image_url


@pytest.mark.asyncio
async def test_media_send_failure_preserves_text(media_plugin_module):
    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, _umo, message_chain):
            self.sent.append(message_chain.chain)
            if any(isinstance(component, Image) for component in message_chain.chain):
                raise RuntimeError("media unavailable")

    context = Context()
    delivery = media_plugin_module.TweetDeliveryService(
        context,
        object(),
        object(),
        _delivery_settings(media_plugin_module),
    )
    chain = [Plain("tweet text"), Image.fromURL("https://example.com/image.jpg")]

    sent = await delivery.send_plain_chain_resilient("session", chain)

    assert sent is True
    assert len(context.sent) == 3
    assert context.sent[1][0].text == "tweet text"


@pytest.mark.asyncio
async def test_primary_and_video_fallback_results_are_reported(media_plugin_module):
    class FailedContext:
        async def send_message(self, _umo, _message_chain):
            raise RuntimeError("adapter unavailable")

    failed_delivery = media_plugin_module.TweetDeliveryService(
        FailedContext(),
        object(),
        object(),
        _delivery_settings(media_plugin_module),
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
    delivery = media_plugin_module.TweetDeliveryService(
        context,
        object(),
        object(),
        _delivery_settings(media_plugin_module),
    )
    sent = await delivery.send_video_or_fallback(
        "session", Video.fromURL("https://example.com/video.mp4")
    )

    assert sent is True
    assert len(context.sent) == 2
    assert context.sent[1][0].text.startswith("视频: ")


@pytest.mark.asyncio
async def test_partial_subscriber_failure_is_reported(media_plugin_module):
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

        async def maybe_translate(self, _tweet_info, _umo, cycle=None):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("tweet")]

    class Context:
        async def send_message(self, umo, _message_chain):
            if umo == "bad":
                raise RuntimeError("send failed")

    delivery = media_plugin_module.TweetDeliveryService(
        Context(),
        Subscriptions(),
        Messages(),
        _delivery_settings(media_plugin_module),
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

    contract = _delivery_contract(media_plugin_module)
    assert result.state is contract.DeliveryState.FAILED


@pytest.mark.asyncio
async def test_text_layout_keeps_retweet_quote_and_translation_paragraphs(
    media_plugin_module,
):
    service = media_plugin_module.TweetMessageService(
        object(),
        object(),
        None,
        _message_settings(
            media_plugin_module,
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
        "正文译文\n\n（由 model 翻译自原文）\n\n"
        "@author (Author) 引用了 @quoted (Quoted) 的帖子\n\n"
        "引用译文\n\n（由 model 翻译自原文）\n\n"
        "https://x.com/author/status/123"
    )


@pytest.mark.asyncio
async def test_screenshot_failure_falls_back_to_text(media_plugin_module):
    async def html_render(*_args, **_kwargs):
        raise RuntimeError("render unavailable")

    service = media_plugin_module.TweetMessageService(
        object(),
        object(),
        html_render,
        _message_settings(
            media_plugin_module,
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
async def test_disabling_separate_media_skips_all_media_work(media_plugin_module):
    class TwitterAPI:
        async def get_remote_file_size(self, _url):
            raise AssertionError("关闭媒体发送后不应探测视频大小")

        async def download_media(self, _url):
            raise AssertionError("关闭媒体发送后不应下载图片")

    service = media_plugin_module.TweetMessageService(
        object(),
        TwitterAPI(),
        None,
        _message_settings(
            media_plugin_module,
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


def test_prepared_delivery_is_shared_by_plain_and_node_modes(media_plugin_module):
    chain = [
        Plain("tweet text"),
        Image.fromURL("https://example.com/image.jpg"),
        Video.fromURL("https://example.com/video.mp4"),
    ]

    plain_delivery = media_plugin_module.TweetDeliveryService(
        object(),
        object(),
        object(),
        _delivery_settings(media_plugin_module),
    )
    plain = plain_delivery.prepare_event_delivery(chain, "Tester")

    node_delivery = media_plugin_module.TweetDeliveryService(
        object(),
        object(),
        object(),
        _delivery_settings(media_plugin_module, use_node=True),
    )
    node = node_delivery.prepare_event_delivery(chain, "Tester")

    assert [type(component) for component in plain.primary_chain] == [Plain, Image]
    assert len(plain.videos) == 1
    assert len(node.primary_chain) == 1
    assert isinstance(node.primary_chain[0], Nodes)
    assert len(node.primary_chain[0].nodes) == 2
    assert len(node.videos) == 1


@pytest.mark.asyncio
async def test_collective_delivery_flushes_and_clears_cache(media_plugin_module):
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

    message_service = media_plugin_module.TweetMessageService(
        object(), object(), None, _message_settings(
            media_plugin_module, text_render_mode="text", pre_download_media=False,
        ),
    )

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

        async def maybe_translate(self, _tweet_info, _umo, cycle=None):
            return None, None

        async def build_message_chain(self, *args, **kwargs):
            return await message_service.build_message_chain(*args, **kwargs)

    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, _umo, message_chain):
            self.sent.append(message_chain.chain)

    context = Context()
    delivery = media_plugin_module.TweetDeliveryService(
        context,
        Subscriptions(),
        Messages(),
        _delivery_settings(
            media_plugin_module,
            use_node=True,
            collective_forward=True,
        ),
    )

    for tweet_id in ("1", "2"):
        queued = await delivery.push_to_subscribers(
            "tester",
            {
                "tweet_id": tweet_id,
                "username": "tester",
                "screen_name": "Tester",
                "text": f"tweet {tweet_id}",
                "images": [f"https://example.com/{tweet_id}.jpg"],
            },
        )

    contract = _delivery_contract(media_plugin_module)
    assert queued.state is contract.DeliveryState.QUEUED
    assert delivery.has_collected is True
    assert context.sent == []

    flushed = await delivery.flush_collected()

    assert delivery.has_collected is False
    assert len(context.sent) == 1
    assert isinstance(context.sent[0][0], Nodes)
    nodes = context.sent[0][0].nodes
    assert len(nodes) == 2
    for tweet_id, node in zip(("1", "2"), nodes):
        assert [type(component) for component in node.content] == [Plain, Image]
        assert f"tweet {tweet_id}" in node.content[0].text
        assert node.content[1].file == f"https://example.com/{tweet_id}.jpg"
    assert flushed.successful_authors == frozenset({"tester"})
    assert flushed.failed_authors == frozenset()


@pytest.mark.asyncio
async def test_failed_collective_retweet_does_not_persist_dedup(media_plugin_module):
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
            return media_plugin_module.SubscriptionService.retweet_seen_by_umo(
                seen_data, umo, tweet_id
            )

        @staticmethod
        def mark_retweet_seen(seen_data, umo, tweet_id):
            media_plugin_module.SubscriptionService.mark_retweet_seen(
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

        async def maybe_translate(self, _tweet_info, _umo, cycle=None):
            return None, None

        async def build_message_chain(self, *_args, **_kwargs):
            return [Plain("retweet")]

    class Context:
        async def send_message(self, _umo, _message_chain):
            raise RuntimeError("adapter unavailable")

    delivery = media_plugin_module.TweetDeliveryService(
        Context(),
        Subscriptions(),
        Messages(),
        _delivery_settings(
            media_plugin_module,
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

    contract = _delivery_contract(media_plugin_module)
    assert queued.state is contract.DeliveryState.QUEUED
    assert saved_seen == []

    flushed = await delivery.flush_collected()

    assert flushed.successful_authors == frozenset()
    assert flushed.failed_authors == frozenset({"tester"})
    assert saved_seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "screenshot"])
@pytest.mark.parametrize("part", ["main", "quote", "both"])
async def test_translation_credit_stays_with_body_before_images(media_plugin_module, mode, part):
    async def render(*_args, **_kwargs):
        raise RuntimeError("render unavailable")

    service = media_plugin_module.TweetMessageService(
        object(), object(), render, _message_settings(
            media_plugin_module, text_render_mode=mode, pre_download_media=False,
            send_media_separately=True, include_tweet_link=False,
        ),
    )
    quote = {"text": "quote original", "images": ["https://example.com/quote.jpg"]}
    if part in ("quote", "both"):
        quote["translated_text"] = "quote translated"
    chain = await service.build_message_chain(
        "tester", {"text": "main original", "images": ["https://example.com/main.jpg"],
                   "quote": quote},
        translated_text="main translated" if part in ("main", "both") else None,
        translate_model="X",
    )
    nodes, _ = media_plugin_module.TweetDeliveryService.split_chain_for_nodes(chain, "Tester")
    assert len(nodes) == 1
    assert [type(component) for component in nodes[0].content] == [Plain, Image, Plain, Image]
    for index, translated in ((0, part in ("main", "both")), (2, part in ("quote", "both"))):
        text = nodes[0].content[index].text
        assert ("（由 X 翻译自原文）" in text) is translated
        assert ("main" if index == 0 else "quote") in text


@pytest.mark.asyncio
@pytest.mark.parametrize("no_text", [False, True])
async def test_successful_screenshot_keeps_separate_nodes(media_plugin_module, monkeypatch, no_text):
    messages = sys.modules[f"{media_plugin_module.__package__}.services.tweet_message_service"]
    monkeypatch.setattr(messages, "tweet_card_page_contexts", lambda context: [context, context])

    async def render(*_args, **_kwargs):
        return "https://example.com/card.png"

    service = media_plugin_module.TweetMessageService(
        object(), object(), render, _message_settings(
            media_plugin_module, no_text=no_text, pre_download_media=False,
        ),
    )
    chain = await service.build_message_chain(
        "tester", {"text": "body", "images": ["https://example.com/main.jpg"]},
    )
    assert chain.group_as_single_node is False
    nodes, videos = media_plugin_module.TweetDeliveryService.split_chain_for_nodes(chain, "Tester")
    assert len(nodes) == (1 if no_text else 3)
    assert all(len(node.content) == 1 for node in nodes)
    assert videos == []


def test_grouped_video_only_chain_has_no_empty_node(media_plugin_module):
    messages = sys.modules[f"{media_plugin_module.__package__}.services.tweet_message_service"]
    video = Video.fromURL("https://example.com/video.mp4")
    chain = messages.TweetMessageChain([video], group_as_single_node=True)
    nodes, videos = media_plugin_module.TweetDeliveryService.split_chain_for_nodes(chain, "Tester")
    assert nodes == []
    assert videos == [video]
