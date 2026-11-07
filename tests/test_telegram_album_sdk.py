"""使用真实 SDK 检查 multipart 请求，不连接 Telegram 或发送真实消息。"""

import base64
import json
import sys

import httpx
import pytest
from test_platform_backoff import clock_for
from test_telegram_support import (
    TG_TOPIC,
    Image,
)
from test_telegram_support import (
    plugin_env as plugin_env,  # noqa: PLC0414 - 复用插件 fixture。
)

telegram = pytest.importorskip("telegram")
from telegram.ext import ExtBot
from telegram.request import BaseRequest, HTTPXRequest


class AlbumRequest(BaseRequest):
    """模拟云端 Bot API，只接受真实文件上传，不接受服务器本地路径。"""

    def __init__(self):
        self.uploads = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **_kwargs):
        if url.endswith("/getMe"):
            result = {"id": 123456, "is_bot": True, "first_name": "Test"}
        else:
            params = request_data.parameters
            files = request_data.multipart_data
            self.uploads.append((params, files))
            if "file://" in json.dumps(params):
                return 400, json.dumps({
                    "ok": False, "error_code": 400,
                    "description": "Bad Request: can't parse InputMedia: local file URI is not allowed",
                }).encode()
            message = {"message_id": 1, "date": 0, "chat": {
                "id": -100123, "type": "supergroup", "title": "Test",
            }}
            result = [message] * len(params["media"]) if "media" in params else message
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_real_sdk_429_retry_preserves_multipart_content(plugin_env, monkeypatch, tmp_path, count):
    _module, make, _data, ordinary, _fail = plugin_env
    plugin = make()
    clock, _module = clock_for(plugin, monkeypatch)
    path = tmp_path / "photo.png"
    path.write_bytes(b"image content")

    async def convert(_image):
        return str(path)

    class ThrottledRequest(AlbumRequest):
        async def do_request(self, url, method, request_data=None, **kwargs):
            result = await super().do_request(url, method, request_data=request_data, **kwargs)
            if not url.endswith("/getMe") and len(self.uploads) == 1:
                return 429, json.dumps({
                    "ok": False, "error_code": 429, "description": "Too Many Requests",
                    "parameters": {"retry_after": 3},
                }).encode()
            return result

    monkeypatch.setattr(Image, "convert_to_file_path", convert, raising=False)
    transport = ThrottledRequest()
    async with ExtBot(token="123456:TEST_TOKEN", request=transport, get_updates_request=transport) as client:
        plugin.context.platform_manager.platform_insts[0].client = client
        assert await plugin.delivery_service.telegram_albums.send(
            TG_TOPIC, [Image(file=str(path)) for _ in range(count)], "link",
        )
    assert clock.sleeps == [3] and len(transport.uploads) == 2
    assert not ordinary
    for _params, files in transport.uploads:
        assert [file[1] for file in files.values()] == [b"image content"] * count


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 10])
@pytest.mark.parametrize("local_mode", [False, True])
async def test_local_images_are_uploaded_as_files_with_real_sdk(
    plugin_env, monkeypatch, tmp_path, count, local_mode,
):
    _module, make, _data, ordinary, _fail = plugin_env
    plugin = make()
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF1kAAAAASUVORK5CYII="
    )
    paths = []
    for index in range(count):
        path = tmp_path / f"截图 {index}.png"
        path.write_bytes(image_bytes)
        paths.append(path)

    async def convert(image):
        return image.file

    monkeypatch.setattr(Image, "convert_to_file_path", convert, raising=False)
    transport = AlbumRequest()
    async with ExtBot(
        token="123456:TEST_TOKEN", request=transport,
        get_updates_request=transport, local_mode=local_mode,
    ) as client:
        plugin.context.platform_manager.platform_insts[0].client = client
        assert await plugin.delivery_service.telegram_albums.send(
            TG_TOPIC, [Image(file=str(path)) for path in paths], "https://x.com/tester/status/101",
        )

    assert not ordinary and len(transport.uploads) == 1
    params, files = transport.uploads[0]
    assert params["chat_id"] == -100123 and params["message_thread_id"] == 42
    assert len(files) == count
    assert [file[1] for file in files.values()] == [image_bytes] * count
    assert "file://" not in json.dumps(params)
    if count > 1:
        assert all(media["media"].startswith("attach://") for media in params["media"])
        assert params["media"][0]["caption"] == "https://x.com/tester/status/101"
    else:
        assert params["caption"] == "https://x.com/tester/status/101"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [
    httpx.ConnectTimeout, httpx.WriteTimeout, httpx.ReadTimeout, httpx.PoolTimeout,
])
async def test_real_sdk_timeout_logs_transport_cause_without_credentials(
    plugin_env, monkeypatch, tmp_path, timeout,
):
    _module, make, _data, _ordinary, _fail = plugin_env
    plugin = make()
    logs = []
    album = plugin.delivery_service.telegram_albums
    module = sys.modules[type(album).__module__]
    monkeypatch.setattr(module.logger, "warning", logs.append)
    path = tmp_path / "photo.png"
    path.write_bytes(b"test image")

    async def convert(_image):
        return str(path)

    async def respond(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 123456, "is_bot": True, "first_name": "Test",
            }})
        raise timeout("secret request URL and credentials", request=request)

    monkeypatch.setattr(Image, "convert_to_file_path", convert, raising=False)
    transport = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})
    async with ExtBot(
        token="123456:TEST_TOKEN", request=transport, get_updates_request=transport,
    ) as client:
        plugin.context.platform_manager.platform_insts[0].client = client
        assert not await album.send(TG_TOPIC, [Image(file="a"), Image(file="b")], "link")

    assert len(logs) == 1
    log = logs[0]
    assert f"causes=TimedOut <- {timeout.__name__}" in log
    assert "stage=send_media_group" in log and "elapsed=" in log
    assert "images=2; prepared=2; bytes=20" in log
    assert "TEST_TOKEN" not in log and "123456" not in log and "secret" not in log
