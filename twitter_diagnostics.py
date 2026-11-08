"""数据源与推送共用的日志关联和异常诊断，不输出请求对象。"""

import hashlib
import json


def reference(value: str) -> str:
    """对会话、媒体地址和分页游标生成关联标记，不记录原值。"""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12] if value else "-"


def anchor(event: str, **fields) -> str:
    """锚点字段仅传入标识和计数，不传消息正文、请求对象或完整 URL。"""
    values = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    )
    return f"[twitter:{event}] {values}".rstrip()


def exception_type_chain(error: BaseException) -> str:
    """保留 SDK 包装前的异常类型，用于区分连接、读写及连接池超时。"""
    names = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(names) < 8:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- ".join(names)
