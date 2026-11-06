import asyncio
import re
import sys
import types
from dataclasses import replace

import pytest
from test_telegram_support import (
    TG_GROUP,
    TG_PRIVATE,
    TG_TOPIC,
    Image,
    Plain,
    make_event,
)
from test_telegram_support import (
    plugin_env as plugin_env,  # noqa: PLC0414 - 复用其他测试模块的 fixture。
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("umo,chat,topic", [
    (TG_GROUP, -100123, None), (TG_TOPIC, -100123, 42), (TG_PRIVATE, 12345, None),
])
@pytest.mark.parametrize("count", [1, 2, 10, 11, 21])
async def test_album_preserves_images_order_caption_and_target(albums, umo, chat, topic, count):
    plugin, _data, ordinary, sent, converted, _client, chain = albums
    images = [Image(file=f"https://example.com/{index}.png") for index in range(count)]
    original = chain(images)
    receipts = []
    assert await plugin.delivery_service.send_telegram_tweet(
        umo, original, "tester", {"tweet_id": "101"},
        on_primary_sent=lambda: receipts.append("sent"),
    )
    assert receipts == ["sent"]
    assert converted == images
    assert not ordinary
    assert sum(len(payload["media"]) if kind == "album" else 1 for kind, payload in sent) == count
    for kind, payload in sent:
        assert payload["chat_id"] == chat
        assert payload.get("message_thread_id") == topic
        if kind == "album":
            assert 2 <= len(payload["media"]) <= 10
            assert payload["media"][0].caption == "https://x.com/tester/status/101"
            assert all(item.caption is None for item in payload["media"][1:])
        else:
            assert payload["caption"] == "https://x.com/tester/status/101"
    assert original[-1].text == "https://x.com/tester/status/101"


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_posts_and_authors_never_share_an_album(albums, collective):
    plugin, data, ordinary, sent, _converted, _client, chain = albums
    plugin.delivery_service.settings = replace(
        plugin.delivery_service.settings, collective_forward=collective,
    )
    data["twitter_subs"] = {
        author: {"subscribers": {TG_TOPIC: {"status": True}}}
        for author in ("alice", "bob")
    }

    async def build(username, tweet_info, *_args, **_kwargs):
        return chain(
            [Image(file=f"{username}-{tweet_info['tweet_id']}-{index}.png") for index in range(2)],
            link=f"https://x.com/{username}/status/{tweet_info['tweet_id']}",
        )

    plugin.message_service.build_message_chain = build
    posts = [("alice", "101"), ("bob", "102"), ("alice", "103")]
    for username, tweet_id in posts:
        result = await plugin.delivery_service.push_to_subscribers(
            username, {"tweet_id": tweet_id, "username": username, "text": "body"},
        )
        assert result.state.value == ("queued" if collective else "delivered")
    if collective:
        assert not sent
        result = await plugin.delivery_service.flush_collected()
        assert not result.failed_authors
        assert len(result.delivered_tweets) == 3
    assert not ordinary
    assert len(sent) == 3
    assert [payload["media"][0].caption for _kind, payload in sent] == [
        f"https://x.com/{username}/status/{tweet_id}" for username, tweet_id in posts
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["正文", "长正文" * 600, "😀" * 510])
async def test_text_is_preserved_in_caption_or_separate_message(albums, body):
    plugin, _data, ordinary, sent, _converted, _client, chain = albums
    original = chain([Image(file="a.png"), Image(file="b.png")], body=body, required=False)
    assert await plugin.delivery_service.send_telegram_tweet(TG_GROUP, original, "tester", {"tweet_id": "101"})
    caption = sent[0][1]["media"][0].caption
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    if ordinary:
        assert ordinary[0][1][0].text == body
        assert caption == "https://x.com/tester/status/101"
    else:
        assert body in caption
    assert original[0].text == body


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_link_switch_controls_album_caption(albums, enabled):
    plugin, _data, ordinary, sent, _converted, _client, chain = albums
    plugin.message_service.settings = replace(plugin.message_service.settings, include_tweet_link=enabled)
    original = chain([Image(file="a.png"), Image(file="b.png")], link="" if not enabled else "https://x.com/tester/status/101")
    assert await plugin.delivery_service.send_telegram_tweet(TG_GROUP, original, "tester", {"tweet_id": "101"})
    assert sent[0][1]["media"][0].caption == ("https://x.com/tester/status/101" if enabled else None)
    assert not ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "screenshot"])
@pytest.mark.parametrize("extra_media", [False, True])
@pytest.mark.parametrize("pre_download", [False, True])
@pytest.mark.parametrize("no_text", [False, True])
async def test_existing_image_selection_quote_order_and_download_are_preserved(
    albums, mode, extra_media, pre_download, no_text,
):
    plugin, _data, _ordinary, sent, converted, _client, _chain = albums
    plugin.message_service.settings = replace(
        plugin.message_service.settings, text_render_mode=mode,
        send_media_separately=extra_media, pre_download_media=pre_download,
        no_text=no_text,
        proxy="http://proxy.test" if pre_download else None,
    )

    async def render(*_args, **_kwargs):
        return "https://render.test/card.png"

    async def download(url):
        return url.encode()

    plugin.message_service.html_render = render
    plugin.twitter_api.download_media = download
    tweet = {"tweet_id": "101", "username": "tester", "text": "正文",
             "images": ["https://img.test/main.png"],
             "quote": {"username": "quoted", "text": "引用正文", "images": ["https://img.test/quote.png"]}}
    original = await plugin.message_service.build_message_chain("tester", tweet)
    selected = [part for part in original if isinstance(part, Image)]
    has_card = mode == "screenshot" and not no_text
    assert len(selected) == (int(has_card) + (2 if extra_media else 0))
    assert await plugin.delivery_service.send_telegram_tweet(TG_GROUP, original, "tester", tweet)
    assert converted == selected
    media = selected[int(has_card):]
    sources = [getattr(part, "data", None) if pre_download else part.file for part in media]
    expected = ["https://img.test/main.png", "https://img.test/quote.png"] if extra_media else []
    assert sources == ([source.encode() for source in expected] if pre_download else expected)
    assert bool(sent) == bool(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sdk", "missing_sdk", "incomplete", "conversion"])
async def test_album_failure_falls_back_without_losing_primary_receipt(albums, monkeypatch, failure):
    plugin, _data, ordinary, _sent, _converted, client, chain = albums

    async def broken(**_payload):
        if failure == "incomplete":
            return []
        raise OSError("SDK failure with secret token")

    client.send_media_group = broken
    if failure == "missing_sdk":
        monkeypatch.setitem(sys.modules, "telegram", None)
    elif failure == "conversion":
        async def fail_convert(_image):
            raise OSError("download failed")
        monkeypatch.setattr(Image, "convert_to_file_path", fail_convert)
    images = [Image(file="a.png"), Image(file="b.png")]
    receipts = []
    assert await plugin.delivery_service.send_telegram_tweet(
        TG_GROUP, chain(images), "tester", {"tweet_id": "101"},
        on_primary_sent=lambda: receipts.append("sent"),
    )
    assert receipts == ["sent"]
    assert [part for part in ordinary[0][1] if isinstance(part, Image)] == images


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [b"GIF87a", b"GIF89a"])
@pytest.mark.parametrize("count", [1, 2])
async def test_gif_keeps_existing_animation_delivery(albums, header, count):
    plugin, _data, ordinary, sent, _converted, _client, chain = albums
    images = [Image(data=header + b"animation")] + [Image(file="b.png")] * (count - 1)
    assert await plugin.delivery_service.send_telegram_tweet(
        TG_GROUP, chain(images), "tester", {"tweet_id": "101"},
    )
    assert not sent
    assert [part for part in ordinary[0][1] if isinstance(part, Image)] == images


@pytest.mark.asyncio
async def test_failed_album_batch_does_not_resend_successful_batch_or_ack_missing_page(albums):
    plugin, _data, ordinary, sent, _converted, client, chain = albums
    images = [Image(file=f"{index}.png") for index in range(12)]
    original_send = client.send_media_group

    async def send_album(**payload):
        if sent:
            raise OSError("second batch failed")
        return await original_send(**payload)

    async def fail_ordinary(_umo, message):
        ordinary.append((_umo, message.chain))
        return False

    client.send_media_group = send_album
    plugin.context.send_message = fail_ordinary
    receipts = []
    assert not await plugin.delivery_service.send_telegram_tweet(
        TG_TOPIC, chain(images), "tester", {"tweet_id": "101"},
        on_primary_sent=lambda: receipts.append("sent"),
    )
    assert len(sent) == 1
    assert receipts == []
    assert all(image not in parts for image in images[:10] for _umo, parts in ordinary)


@pytest.mark.asyncio
@pytest.mark.parametrize("required_count", [2, 12])
@pytest.mark.parametrize("stage", ["album", "fallback"])
async def test_cancellation_saves_receipt_only_after_all_screenshot_pages(albums, required_count, stage):
    plugin, data, _ordinary, sent, _converted, client, chain = albums
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_TOPIC: {"status": True}}}}
    images = [Image(file=f"{index}.png") for index in range(12)]
    original = chain(images)
    original.required_images = tuple(images[:required_count])

    async def build(*_args, **_kwargs):
        return original

    send = client.send_media_group

    async def cancel(**payload):
        if stage == "fallback":
            raise OSError("album unavailable")
        if sent:
            raise asyncio.CancelledError()
        return await send(**payload)

    async def cancel_fallback(_umo, message):
        if len(message.chain) > 1 and any(part in original.required_images for part in message.chain):
            return False
        if len(message.chain) == 1 and message.chain[0] in original.required_images:
            return True
        raise asyncio.CancelledError()

    plugin.message_service.build_message_chain = build
    client.send_media_group = cancel
    if stage == "fallback":
        plugin.context.send_message = cancel_fallback
    with pytest.raises(asyncio.CancelledError):
        await plugin.delivery_service.push_to_subscribers(
            "tester", {"tweet_id": "101", "username": "tester", "text": "body"},
            pending_tweet_items={"tester": [{"tweet_id": "101", "username": "tester"}]},
        )
    author = data["twitter_subs"]["tester"]
    assert author["since_id"] == "100"
    assert author["subscribers"][TG_TOPIC].get("pending_delivery_ids", []) == (["101"] if required_count == 2 else [])
    assert [(item["tweet_id"], item["username"]) for item in author["pending_tweet_items"]] == [("101", "tester")]


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_failed_album_keeps_cursor_and_retries_only_unconfirmed_chat(albums, collective):
    plugin, data, ordinary, sent, _converted, client, chain = albums
    plugin.delivery_service.settings = replace(plugin.delivery_service.settings, collective_forward=collective)
    data["twitter_subs"] = {"tester": {
        "since_id": "100", "subscribers": {TG_TOPIC: {"status": True}, TG_PRIVATE: {"status": True}},
    }}
    failing = True
    send = client.send_media_group
    send_ordinary = plugin.context.send_message

    async def album(**payload):
        if failing and payload.get("message_thread_id") == 42:
            raise OSError("topic unavailable")
        return await send(**payload)

    async def ordinary_message(umo, message):
        if failing and umo == TG_TOPIC:
            return False
        return await send_ordinary(umo, message)

    async def build(*_args, **_kwargs):
        return chain([Image(file="a.png"), Image(file="b.png")])

    async def timeline(_username, since_id):
        return [{"tweet_id": "101", "username": "tester"}] if since_id == "100" else []

    async def tweet(*_args):
        return {"tweet_id": "101", "username": "tester", "text": "body", "status": True}

    client.send_media_group = album
    plugin.context.send_message = ordinary_message
    plugin.message_service.build_message_chain = build
    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = tweet
    await plugin.polling_service.check_all()
    author = data["twitter_subs"]["tester"]
    assert author["since_id"] == "100"
    assert author["subscribers"][TG_PRIVATE]["pending_delivery_ids"] == ["101"]
    assert not author["subscribers"][TG_TOPIC].get("pending_delivery_ids")
    assert len(sent) == 1 and sent[0][1]["chat_id"] == 12345

    failing = False
    sent.clear()
    ordinary.clear()
    await plugin.polling_service.check_all()
    assert data["twitter_subs"]["tester"]["since_id"] == "101"
    assert len(sent) == 1 and sent[0][1]["message_thread_id"] == 42
    assert not ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["test", "parse"])
async def test_direct_commands_send_one_album_with_link(albums, command):
    plugin, _data, ordinary, sent, _converted, _client, chain = albums

    async def timeline(_username):
        return [{"tweet_id": "101", "username": "tester"}]

    async def tweet(*_args):
        return {"status": True, "tweet_id": "101", "username": "tester", "text": "body"}

    async def build(*_args, **_kwargs):
        return chain([Image(file="a.png"), Image(file="b.png")])

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = tweet
    plugin.message_service.build_message_chain = build
    event = make_event(TG_TOPIC, "https://x.com/tester/status/101")
    if command == "test":
        results = [item async for item in plugin.test_tweet(event, "tester")]
    else:
        results = [item async for item in plugin._handle_tweet_link(event, re.search(
            r"(https://x.com/(tester)/status/(101))", event.message_str,
        ), report_errors=True)]
    assert not any(isinstance(item, list) for item in results)
    assert not ordinary
    assert len(sent) == 1
    assert sent[0][1]["message_thread_id"] == 42
    assert sent[0][1]["media"][0].caption == "https://x.com/tester/status/101"


@pytest.mark.asyncio
async def test_bad_request_logs_reason_without_exposing_token_or_urls(albums, monkeypatch):
    plugin, _data, _ordinary, _sent, _converted, client, _chain = albums
    logs = []
    module = sys.modules[type(plugin.delivery_service.telegram_albums).__module__]
    monkeypatch.setattr(module.logger, "warning", logs.append)
    client.token = "123456:SECRET_TOKEN"

    class BadRequest(Exception):
        message = (
            "Wrong file identifier https://api.telegram.org/bot123456:SECRET_TOKEN/sendMediaGroup "
            "token=123456:SECRET_TOKEN file:///private/local.png\nmore details"
        )

    async def reject(**_payload):
        raise BadRequest()

    client.send_media_group = reject
    assert not await plugin.delivery_service.telegram_albums.send(
        TG_GROUP, [Image(file="a.png"), Image(file="b.png")], "link",
    )
    assert len(logs) == 1 and "Wrong file identifier" in logs[0]
    assert "SECRET_TOKEN" not in logs[0] and "123456" not in logs[0]
    assert "api.telegram.org" not in logs[0] and "private/local.png" not in logs[0]
    assert "\n" not in logs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_upload_handles_close_after_send_failure_or_cancellation(albums, count, outcome):
    plugin, _data, _ordinary, _sent, _converted, client, _chain = albums
    handles = []

    async def send(**payload):
        handles.extend([payload["photo"]] if count == 1 else [item.media for item in payload["media"]])
        assert all(not handle.closed and handle.read() == b"image" for handle in handles)
        if outcome == "failure":
            raise OSError("upload failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return object() if count == 1 else [object()] * count

    client.send_photo = client.send_media_group = send
    pending = plugin.delivery_service.telegram_albums.send(
        TG_GROUP, [Image(file=f"{index}.png") for index in range(count)], "link",
    )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        assert await pending is (outcome == "success")
    assert len(handles) == count and all(handle.closed for handle in handles)


@pytest.mark.asyncio
async def test_album_cleans_materialized_temp_files_but_keeps_local(albums, monkeypatch, tmp_path):
    plugin, _data, _ordinary, _sent, _converted, _client, chain = albums
    created = {}

    async def convert(image):
        path = tmp_path / f"resolved-{len(created)}.png"
        path.write_bytes(b"image")
        created[id(image)] = path
        return str(path)

    monkeypatch.setattr(Image, "convert_to_file_path", convert, raising=False)
    remote = Image(file="https://example.com/remote.png")
    encoded = Image(file="base64://aW1hZ2U=")
    local = Image(file=str(tmp_path / "local.png"))
    (tmp_path / "local.png").write_bytes(b"local")

    assert await plugin.delivery_service.send_telegram_tweet(
        TG_GROUP, chain([remote, encoded, local], required=False),
        "tester", {"tweet_id": "101"},
    )
    # 远端与 base64 图片会下载为临时文件，发送后必须回收。
    assert not created[id(remote)].exists()
    assert not created[id(encoded)].exists()
    # 本地截图文件不属于本服务，不能误删。
    assert created[id(local)].exists()


@pytest.mark.asyncio
async def test_image_download_timeout_logs_preparation_stage(albums, monkeypatch):
    plugin, _data, _ordinary, sent, _converted, _client, _chain = albums
    album = plugin.delivery_service.telegram_albums
    logs = []
    module = sys.modules[type(album).__module__]
    monkeypatch.setattr(module.logger, "warning", logs.append)

    async def fail_convert(_image):
        raise TimeoutError("secret image URL")

    monkeypatch.setattr(Image, "convert_to_file_path", fail_convert)
    assert not await album.send(TG_GROUP, [Image(file="a.png")], "link")
    assert not sent
    assert len(logs) == 1 and "stage=prepare_images" in logs[0]
    assert "prepared=0; bytes=0" in logs[0] and "causes=TimeoutError" in logs[0]
    assert "secret" not in logs[0]
