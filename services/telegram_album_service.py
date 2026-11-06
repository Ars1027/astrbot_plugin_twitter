"""通过已加载的 Telegram 适配器发送单帖图片，不新增平台依赖。"""

import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .platform_support import meta_value, platform_instances


class TelegramAlbumService:
    """仅负责一批图片的 SDK 调用；降级和推文回执由发送服务统一处理。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def send(self, umo: str, images: list, caption: str) -> bool:
        """发送 1 张照片或 2～10 张相册，保留群、话题和私聊路由。"""
        client = None
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
            # 与 AstrBot 图片适配器使用同一转换入口，兼容 URL、本地截图和预下载图片。
            paths = [Path(await image.convert_to_file_path()) for image in images]
            for path in paths:
                if path.suffix.lower() == ".gif":
                    return False
                with path.open("rb") as image_file:
                    if image_file.read(6) in (b"GIF87a", b"GIF89a"):
                        # 照片相册不支持动画，交回原适配器发送 GIF。
                        return False
            with ExitStack() as stack:
                # InputMediaPhoto 会把 Path 转成 file:// URI；云端 Bot API 无法读取
                # 插件服务器的本地文件。传二进制文件对象，强制 SDK 上传 multipart。
                files = [stack.enter_context(path.open("rb")) for path in paths]
                if len(files) == 1:
                    result = await client.send_photo(
                        **target, photo=files[0], caption=caption or None, parse_mode=None,
                    )
                    return result is not None and result is not False

                # Telegram SDK 由官方适配器提供；QQ 等平台加载插件时不导入它。
                from telegram import InputMediaPhoto

                media = [
                    InputMediaPhoto(
                        media=image_file, caption=(caption or None) if index == 0 else None,
                        parse_mode=None,
                    )
                    for index, image_file in enumerate(files)
                ]
                result = await client.send_media_group(**target, media=media)
                return (
                    isinstance(result, (list, tuple)) and len(result) == len(images)
                    and all(item is not None and item is not False for item in result)
                )
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
            logger.warning(f"Telegram 相册发送失败，回退到普通消息: {reason}")
            return False
