"""头像缓存、受限下载和截图接入的回归测试。"""

import asyncio
import base64
import copy
import hashlib
import json
import os
import threading
import time

import httpx
import pytest

from test_fxtwitter_api import api_module as api_module
from test_media_lifecycle import (
    Image,
    _message_settings,
    plugin_module as plugin_module,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AwMCAO+aZfQAAAAASUVORK5CYII="
)
URL = "https://pbs.twimg.com/profile_images/avatar.png"


class API:
    def __init__(self):
        self.calls = []
        self.result = PNG
        self.error = None

    async def download_media(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.result


def cache_file(tmp_path, url=URL):
    key = hashlib.sha256(url.encode()).hexdigest()
    return tmp_path / "avatar_cache" / f"{key}.json"


def expire_file(path):
    record = json.loads(path.read_text())
    record["fetched_at"] = time.time() - 8 * 86400
    path.write_text(json.dumps(record))


@pytest.mark.asyncio
async def test_cache_survives_reload_and_refreshes_expired_avatar(
    plugin_module, tmp_path
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    uri = await cache.get(URL)
    assert uri.startswith("data:image/png;base64,")
    assert await cache.get(URL) == uri
    assert len(api.calls) == 1
    assert api.calls[0][1] == {"timeout": 5, "max_bytes": 1024 * 1024}
    await cache.close()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    assert await cache.get(URL) == uri and len(api.calls) == 1
    expire_file(cache_file(tmp_path))
    api.result = b"GIF89a-new-avatar"
    refreshed = await cache.get(URL)
    assert refreshed.startswith("data:image/gif;") and len(api.calls) == 2
    assert refreshed != uri


@pytest.mark.asyncio
async def test_stale_cache_survives_failure_and_new_url_does_not_reuse_it(
    plugin_module, tmp_path
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    uri = await cache.get(URL)
    expire_file(cache_file(tmp_path))
    api.error = httpx.ConnectError("offline")
    assert await cache.get(URL) == uri
    assert await cache.get(URL) == uri
    assert len(api.calls) == 2
    assert await cache.get(URL + "?new=1") is None
    assert len(api.calls) == 3
    assert not cache_file(tmp_path, URL + "?new=1").exists()
    assert await cache.get(URL) == uri


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"<html>unavailable</html>",
        b"not image",
        b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024),
    ],
    ids=["empty", "html", "invalid", "oversized"],
)
async def test_invalid_or_oversized_response_not_cached(
    plugin_module, tmp_path, invalid
):
    api = API()
    api.result = invalid
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    assert await cache.get(URL) is None
    assert await cache.get(URL) is None
    assert len(api.calls) == 1
    assert not cache_file(tmp_path).exists()


@pytest.mark.asyncio
async def test_corrupt_cache_and_write_failure_are_nonfatal(
    plugin_module, tmp_path, monkeypatch
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    path = cache_file(tmp_path)
    path.parent.mkdir()
    path.write_text("broken json")
    assert await cache.get(URL)
    assert len(api.calls) == 1

    record = json.loads(path.read_text())
    record["data"] = base64.b64encode(PNG + b"corrupted").decode()
    path.write_text(json.dumps(record))
    assert await cache.get(URL)
    assert len(api.calls) == 2

    def denied(*_args):
        raise PermissionError("read only")

    monkeypatch.setattr(cache, "_write", denied)
    assert await cache.get(URL + "?new")
    assert not cache_file(tmp_path, URL + "?new").exists()
    # 连目录都无法创建时，也不能让截图正文丢失。
    inaccessible = plugin_module.AvatarCacheService(api, denied)
    assert await inaccessible.get(URL)


@pytest.mark.asyncio
async def test_same_url_single_flight_and_one_waiter_cancellation(
    plugin_module, tmp_path
):
    entered, release = asyncio.Event(), asyncio.Event()
    api = API()

    async def download(url, **kwargs):
        api.calls.append(url)
        entered.set()
        await release.wait()
        return PNG

    api.download_media = download
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    first = asyncio.create_task(cache.get(URL))
    await entered.wait()
    second = asyncio.create_task(cache.get(URL))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second
    assert len(api.calls) == 1 and not cache._inflight and not cache._waiters


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["timeout", "cancel", "close"])
async def test_timeout_cancellation_and_close_leave_no_download_tasks(
    plugin_module, tmp_path, action
):
    entered = asyncio.Event()
    cancelled = []
    api = API()

    async def download(url, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    api.download_media = download
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    cache.DOWNLOAD_TIMEOUT = 0.05
    tasks_before = asyncio.all_tasks()
    task = asyncio.create_task(cache.get(URL))
    await entered.wait()
    if action == "timeout":
        assert await task is None
        assert len(cache._negative) == 1
    else:
        if action == "close":
            await cache.close()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled == [True]
    assert not cache._inflight and not cache._waiters
    assert asyncio.all_tasks() == tasks_before
    assert not list((tmp_path / "avatar_cache").glob("*.tmp"))


@pytest.mark.asyncio
async def test_negative_cache_is_bounded_and_expires(plugin_module, tmp_path):
    api = API()
    api.error = ValueError("offline")
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    cache.MAX_ENTRIES = 3
    for i in range(5):
        assert await cache.get(URL + str(i)) is None
    assert len(cache._negative) == 3
    key = hashlib.sha256((URL + "4").encode()).hexdigest()
    cache._negative[key] = time.monotonic() - 1
    api.error = None
    assert await cache.get(URL + "4")
    assert key not in cache._negative


@pytest.mark.asyncio
async def test_cancellation_waits_for_atomic_disk_write(
    plugin_module, tmp_path, monkeypatch
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    started, release = threading.Event(), threading.Event()
    original_write = cache._write

    def slow_write(*args):
        started.set()
        assert release.wait(2)
        original_write(*args)

    monkeypatch.setattr(cache, "_write", slow_write)
    task = asyncio.create_task(cache.get(URL))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cache._inflight
    assert not list((tmp_path / "avatar_cache").glob("*.tmp"))
    assert await cache.get(URL)
    assert len(api.calls) == 1


@pytest.mark.asyncio
async def test_lru_and_byte_capacity_only_clean_avatar_directory(
    plugin_module, tmp_path
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    cache.MAX_ENTRIES = 3
    for i in range(3):
        await cache.get(URL + str(i))
        os.utime(cache_file(tmp_path, URL + str(i)), (100 + i, 100 + i))
    await cache.get(URL + "0")  # 命中应延长最近使用时间，但不重置七天有效期。
    outside = tmp_path / "keep.txt"
    outside.write_text("do not delete")
    inside = tmp_path / "avatar_cache" / "keep.txt"
    inside.write_text("not a cache entry")
    await cache.get(URL + "3")
    assert cache_file(tmp_path, URL + "0").exists()
    assert not cache_file(tmp_path, URL + "1").exists()
    assert len(list((tmp_path / "avatar_cache").glob("*.json"))) == 3
    assert outside.exists() and inside.exists()

    cache.MAX_DISK_BYTES = 450
    for i in range(4, 10):
        await cache.get(URL + str(i))
        assert (
            sum(p.stat().st_size for p in (tmp_path / "avatar_cache").glob("*.json"))
            <= 450
        )
        assert not list((tmp_path / "avatar_cache").glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_download", [False, True])
@pytest.mark.parametrize("send_media", [False, True])
async def test_screenshot_caches_both_avatars_without_changing_original_media(
    plugin_module, tmp_path, pre_download, send_media
):
    api = API()
    captured = {}
    tweet = {
        "text": "正文",
        "avatar": URL,
        "images": ["https://example.test/photo.png"],
        "quote": {"text": "引用", "avatar": URL + "?quote", "images": []},
    }
    original = copy.deepcopy(tweet)

    async def data_uri(url):
        return "data:image/png;base64," + base64.b64encode(PNG).decode()

    async def render(_template, context, options):
        captured.update(context)
        return "https://example.test/rendered.png"

    api.download_media_to_data_uri = data_uri
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    service = plugin_module.TweetMessageService(
        object(),
        api,
        render,
        _message_settings(
            plugin_module,
            pre_download_media=pre_download,
            send_media_separately=send_media,
        ),
        avatar_cache=cache,
    )
    chain = await service.build_message_chain("user", tweet)
    assert captured["tweet"]["avatar"].startswith("data:image/png;")
    assert captured["tweet"]["quote"]["avatar"].startswith("data:image/png;")
    assert tweet == original
    assert sum(isinstance(part, Image) for part in chain) == (2 if send_media else 1)
    assert captured["tweet"]["media"][0]["url"].startswith(
        "data:" if pre_download else "https:"
    )


@pytest.mark.asyncio
async def test_text_mode_never_loads_avatars_and_failed_render_stays_media_disabled(
    plugin_module, tmp_path
):
    api = API()
    cache = plugin_module.AvatarCacheService(api, lambda: tmp_path)
    tweet = {
        "text": "正文",
        "avatar": URL,
        "images": ["https://example.test/photo.png"],
    }
    service = plugin_module.TweetMessageService(
        object(),
        api,
        None,
        _message_settings(
            plugin_module, text_render_mode="text", send_media_separately=False
        ),
        avatar_cache=cache,
    )
    chain = await service.build_message_chain("user", tweet)
    assert len(chain) == 1 and "正文" in chain[0].text and not api.calls

    async def failed_render(*_args, **_kwargs):
        raise ValueError("render failed")

    service = plugin_module.TweetMessageService(
        object(),
        api,
        failed_render,
        _message_settings(
            plugin_module, send_media_separately=False, pre_download_media=False
        ),
        avatar_cache=cache,
    )
    chain = await service.build_message_chain("user", tweet)
    assert len(chain) == 1 and "正文" in chain[0].text
    assert len(api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["nitter", "fxtwitter"])
@pytest.mark.parametrize("declared_size", [None, "1000000", "10"])
async def test_stream_limit_with_or_without_content_length_keeps_provider_ready(
    api_module, provider, declared_size
):
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 80
            yield b"y" * 80

        async def aclose(self):
            closed.append(True)

    def handle(request):
        assert request.extensions["timeout"]["read"] == 5
        return httpx.Response(
            200,
            headers={"Content-Length": declared_size} if declared_size else {},
            stream=Body(),
        )

    api = api_module.TwitterAPI(provider=provider, nitter_url="https://nitter.test")
    api.provider_ready = True
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with pytest.raises(ValueError, match="大小限制"):
        await api.download_media(URL, timeout=5, max_bytes=100)
    assert api.is_ready and api.provider_ready and closed
    await api.close()


@pytest.mark.asyncio
async def test_media_download_without_limit_keeps_existing_behavior(api_module):
    api = api_module.TwitterAPI()
    api._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=PNG))
    )
    assert await api.download_media(URL) == PNG
    assert await api.download_media(URL, timeout=5, max_bytes=1024) == PNG
    await api.close()
