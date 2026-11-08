"""通过已加载的 Telegram 适配器发送单帖图片，不新增平台依赖。"""

import re
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter
from typing import Any

from astrbot.api import logger

from ..twitter_diagnostics import (
    anchor,
    exception_type_chain,
    reference,
)
from .platform_send_backoff import PlatformRateLimited, PlatformSendBackoff
from .platform_support import meta_value, platform_instances


class TelegramAlbumService:
    """仅负责一批图片的 SDK 调用；降级和推文回执由发送服务统一处理。"""

    def __init__(self, context: Any, backoff: PlatformSendBackoff | None = None) -> None:
        self.context = context
        self.backoff = backoff if backoff is not None else PlatformSendBackoff()

    @staticmethod
    def _materializes_temp_file(image: Any) -> bool:
        """判断图片是否由本服务转成临时文件，临时文件由本服务负责清理。"""
        source = str(getattr(image, "file", "") or "")
        if not source:
            source = str(getattr(image, "url", "") or "")
        return source.startswith(("http://", "https://", "base64://", "data:"))

    @staticmethod
    def _cleanup_materialized_files(paths: list[Path]) -> None:
        """删除远端或 base64 下载产生的临时文件；本地截图继续保留。"""
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(f"清理 Telegram 相册临时文件失败: {type(exc).__name__}")

    async def send(self, umo: str, images: list, caption: str) -> bool:
        """发送 1 张照片或 2～10 张相册，保留群、话题和私聊路由。"""
        client = None
        started = stage_started = perf_counter()
        stage = "resolve_platform"
        paths = []
        materialized_paths: list[Path] = []
        total_bytes = 0
        try:
            platform_id, _message_type, session_id = umo.split(":", 2)
            platform = next(
                item for item in platform_instances(self.context)
                if meta_value(item.meta(), "id") == platform_id
                and meta_value(item.meta(), "name") == "telegram"
            )
            client = platform.get_client()
            chat_id, separator, topic_id = session_id.partition("#")
            target = {"chat_id": int(chat_id)}
            if separator:
                target["message_thread_id"] = int(topic_id)
            stage, stage_started = "prepare_images", perf_counter()
            # 与 AstrBot 图片适配器使用同一转换入口，兼容 URL、本地截图和预下载图片。
            for image in images:
                path = Path(await image.convert_to_file_path())
                total_bytes += path.stat().st_size
                paths.append(path)
                if self._materializes_temp_file(image):
                    materialized_paths.append(path)
            stage, stage_started = "check_images", perf_counter()
            for path in paths:
                if path.suffix.lower() == ".gif":
                    logger.debug(anchor("tg.album.skip", reason="gif", target=reference(umo)))
                    return False
                with path.open("rb") as image_file:
                    if image_file.read(6) in (b"GIF87a", b"GIF89a"):
                        # 照片相册不支持动画，交回原适配器发送 GIF。
                        logger.debug(anchor("tg.album.skip", reason="gif_header", target=reference(umo)))
                        return False
            with ExitStack() as stack:
                stage, stage_started = "build_upload", perf_counter()
                # InputMediaPhoto 会把 Path 转成 file:// URI；云端 Bot API 无法读取
                # 插件服务器的本地文件。传二进制文件对象，强制 SDK 上传 multipart。
                files = [stack.enter_context(path.open("rb")) for path in paths]
                if len(files) == 1:
                    stage, stage_started = "send_photo", perf_counter()
                    async def send_photo():
                        files[0].seek(0)
                        return await client.send_photo(
                            **target, photo=files[0], caption=caption or None, parse_mode=None,
                        )

                    result = await self.backoff.run(
                        umo, send_photo,
                    )
                    succeeded = result is not None and result is not False
                    return succeeded

                # Telegram SDK 由官方适配器提供；QQ 等平台加载插件时不导入它。
                from telegram import InputMediaPhoto

                async def send_media_group():
                    # SDK 可能已读取上传流；重试时重新构建媒体并复位文件指针。
                    for image_file in files:
                        image_file.seek(0)
                    media = [
                        InputMediaPhoto(
                            media=image_file, caption=(caption or None) if index == 0 else None,
                            parse_mode=None,
                        )
                        for index, image_file in enumerate(files)
                    ]
                    return await client.send_media_group(**target, media=media)
                stage, stage_started = "send_media_group", perf_counter()
                result = await self.backoff.run(umo, send_media_group)
                succeeded = (
                    isinstance(result, (list, tuple)) and len(result) == len(images)
                    and all(item is not None and item is not False for item in result)
                )
                if not succeeded:
                    logger.warning(anchor("tg.upload.incomplete", target=reference(umo), expected=len(images)))
                return succeeded
        except PlatformRateLimited:
            # 明确限流不能退回普通消息继续发送；上层保留真实回执与游标。
            raise
        except Exception as exc:  # noqa: BLE001 - 可选 SDK 的异常在平台边界统一降级。
            reason = type(exc).__name__
            if reason == "BadRequest":
                # 保留 API 拒绝原因以便排查，过滤 Token、URL 和多行内容。
                detail = str(getattr(exc, "message", ""))
                token = getattr(client, "token", "")
                if isinstance(token, str) and token:
                    detail = detail.replace(token, "[TOKEN]")
                detail = re.sub(r"(?:https?|file)://\S+", "[URL]", detail)
                detail = re.sub(r"\b\d+:[A-Za-z0-9_-]+\b", "[TOKEN]", detail)
                reason += f": {' '.join(detail.split())[:300]}"
            logger.warning(
                f"Telegram 相册发送失败，回退到普通消息: {reason}; "
                f"stage={stage}; elapsed={perf_counter() - started:.2f}s; "
                f"stage_elapsed={perf_counter() - stage_started:.2f}s; "
                f"images={len(images)}; prepared={len(paths)}; bytes={total_bytes}; "
                f"causes={exception_type_chain(exc)}; "
                f"{anchor('tg.album.failed', target=reference(umo))}"
            )
            return False
        finally:
            # 发送或中断后都要回收本服务下载的临时文件，避免轮询长期累积。
            self._cleanup_materialized_files(materialized_paths)
