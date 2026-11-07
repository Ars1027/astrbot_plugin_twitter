"""平台实例查询；通过适配器类型判断能力，不依赖用户自定义实例 ID。"""

from typing import Any


def platform_instances(context: Any) -> list:
    manager = getattr(context, "platform_manager", None)
    instances = getattr(manager, "platform_insts", None)
    if instances is None:
        get_insts = getattr(manager, "get_insts", None)
        instances = get_insts() if callable(get_insts) else []
    return list(instances or [])


def meta_value(meta: Any, field: str) -> str:
    value = meta.get(field) if isinstance(meta, dict) else getattr(meta, field, None)
    return str(value or "")


def is_telegram_target(context: Any, umo: str) -> bool:
    platform_id = umo.split(":", 1)[0]
    return any(
        meta_value(platform.meta(), "id") == platform_id
        and meta_value(platform.meta(), "name") == "telegram"
        for platform in platform_instances(context)
    )


def is_telegram_event(event: Any) -> bool:
    get_name = getattr(event, "get_platform_name", None)
    return callable(get_name) and get_name() == "telegram"
