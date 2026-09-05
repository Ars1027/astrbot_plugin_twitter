"""截图头像的小容量持久化缓存，不缓存推文原始媒体。"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger


class AvatarCacheService:
    """按完整 URL 缓存头像，刷新失败可使用同 URL 的旧头像。"""

    MAX_ENTRIES = 200
    MAX_DISK_BYTES = 16 * 1024 * 1024
    MAX_IMAGE_BYTES = 1024 * 1024
    TTL_SECONDS = 7 * 24 * 60 * 60
    DOWNLOAD_TIMEOUT = 5
    NEGATIVE_TTL = 60
    _FILE_NAME = re.compile(r"[0-9a-f]{64}\.(?:json|tmp)")

    def __init__(self, api: Any, data_dir: Callable[[], Path]) -> None:
        self.api = api
        self._data_dir = data_dir
        self._io_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task] = {}
        self._waiters: dict[str, int] = {}
        self._negative: OrderedDict[str, float] = OrderedDict()
        self._closed = False

    @staticmethod
    def _image_mime(data: bytes) -> str:
        """检查图片签名而非信任扩展名，解码失败由模板占位兜底。"""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        raise ValueError("头像响应不是支持的图片格式")

    @classmethod
    def _data_uri(cls, data: bytes) -> str:
        if not data or len(data) > cls.MAX_IMAGE_BYTES:
            raise ValueError("头像大小超出限制或内容为空")
        return f"data:{cls._image_mime(data)};base64,{base64.b64encode(data).decode('ascii')}"

    async def get(self, url: str) -> str | None:
        """返回内嵌头像；同 URL 的并发调用共用一次读取或下载。"""
        url = str(url or "").strip()
        if self._closed or not url.startswith(("http://", "https://")):
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._resolve(key, url))
            self._inflight[key] = task
            self._waiters[key] = 0
        self._waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            self._waiters[key] -= 1
            if not self._waiters[key]:
                self._waiters.pop(key)
                self._inflight.pop(key)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        """卸载时取消并等待在途下载，避免遗留刷新任务。"""
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._negative.clear()

    async def _disk(self, operation: Callable, *args):
        async with self._io_lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 线程不能被取消；等原子读写结束后再释放锁，避免卸载时留下半写文件。
                await asyncio.gather(task, return_exceptions=True)
                raise

    async def _resolve(self, key: str, url: str) -> str | None:
        cached = None
        try:
            cached = await self._disk(self._read, key)
        except Exception as exc:
            logger.debug(f"头像缓存读取失败: {type(exc).__name__}")
        if cached and time.time() - cached[1] < self.TTL_SECONDS:
            return cached[0]

        now = time.monotonic()
        for old_key, expires in list(self._negative.items()):
            if expires <= now:
                self._negative.pop(old_key, None)
        if self._negative.get(key, 0) > now:
            return cached[0] if cached else None

        try:
            data = await asyncio.wait_for(
                self.api.download_media(
                    url, timeout=self.DOWNLOAD_TIMEOUT, max_bytes=self.MAX_IMAGE_BYTES
                ),
                timeout=self.DOWNLOAD_TIMEOUT,
            )
            uri = self._data_uri(data)
        except Exception as exc:
            self._negative[key] = time.monotonic() + self.NEGATIVE_TTL
            self._negative.move_to_end(key)
            while len(self._negative) > self.MAX_ENTRIES:
                self._negative.popitem(last=False)
            logger.debug(f"头像下载失败，使用旧缓存或占位: {type(exc).__name__}")
            return cached[0] if cached else None

        try:
            await self._disk(self._write, key, data)
        except Exception as exc:
            logger.debug(
                f"头像缓存保存失败，本次仍使用已下载头像: {type(exc).__name__}"
            )
        return uri

    def _directory(self) -> Path:
        directory = Path(self._data_dir()) / "avatar_cache"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _prune(self, directory: Path, reserve_bytes: int = 0, reserve_entries: int = 0):
        entries = []
        for path in directory.iterdir():
            if not self._FILE_NAME.fullmatch(path.name) or path.is_symlink():
                continue
            if path.suffix == ".tmp":
                path.unlink(missing_ok=True)
            elif path.is_file():
                entries.append((path, path.stat()))
        entries.sort(key=lambda entry: entry[1].st_mtime_ns)
        size = sum(stat.st_size for _, stat in entries)
        count = len(entries)
        for path, stat in entries:
            if (
                size + reserve_bytes <= self.MAX_DISK_BYTES
                and count + reserve_entries <= self.MAX_ENTRIES
            ):
                break
            path.unlink(missing_ok=True)
            size -= stat.st_size
            count -= 1

    def _read(self, key: str) -> tuple[str, float] | None:
        directory = self._directory()
        self._prune(directory)
        path = directory / f"{key}.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            with path.open("rb") as file:
                payload = file.read(self.MAX_IMAGE_BYTES * 2 + 1)
            if len(payload) > self.MAX_IMAGE_BYTES * 2:
                raise ValueError("头像缓存过大")
            record = json.loads(payload)
            fetched_at = float(record["fetched_at"])
            if not 0 < fetched_at <= time.time() + 300:
                raise ValueError("头像缓存时间无效")
            data = base64.b64decode(record["data"], validate=True)
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("头像缓存内容校验失败")
            uri = self._data_uri(data)
        except (ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return None
        try:
            path.touch()
        except OSError:
            pass
        return uri, fetched_at

    def _write(self, key: str, data: bytes) -> None:
        directory = self._directory()
        payload = json.dumps(
            {
                "fetched_at": time.time(),
                "data": base64.b64encode(data).decode("ascii"),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ).encode("utf-8")
        if len(payload) > self.MAX_DISK_BYTES:
            return
        # 写入前预留临时文件空间，连同替换前的旧文件一起计入容量。
        self._prune(directory, len(payload), 1)
        path = directory / f"{key}.json"
        temporary = directory / f"{key}.tmp"
        try:
            with temporary.open("xb") as file:
                file.write(payload)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
