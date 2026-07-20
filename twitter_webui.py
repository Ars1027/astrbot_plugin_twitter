"""Twitter 订阅管理 Plugin Page 的后端接口。"""

import asyncio
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


PLUGIN_NAME = "astrbot_plugin_twitter"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MESSAGE_TYPES = {"GroupMessage", "FriendMessage", "OtherMessage"}
GROUP_LIST_TIMEOUT_SECONDS = 6


class TwitterWebUIController:
    """注册并实现 Twitter 订阅管理页面所需的 Web API。"""

    def __init__(self, plugin: Any, context: Any):
        self.plugin = plugin
        self.context = context
        self._register_routes()

    def _register_routes(self) -> None:
        routes = (
            ("overview", self.overview, ["GET"], "Twitter 订阅概览"),
            (
                "settings/poll-interval",
                self.save_poll_interval,
                ["POST"],
                "修改 Twitter 轮询间隔",
            ),
            (
                "subscriptions/add",
                self.add_subscription,
                ["POST"],
                "新增 Twitter 订阅",
            ),
            (
                "subscriptions/update",
                self.update_subscription,
                ["POST"],
                "修改 Twitter 订阅",
            ),
            (
                "subscriptions/group-status",
                self.update_group_status,
                ["POST"],
                "统一修改群聊 Twitter 推送状态",
            ),
            (
                "subscriptions/remove",
                self.remove_subscription,
                ["POST"],
                "移除 Twitter 订阅",
            ),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    @staticmethod
    def _meta_value(meta: Any, field: str, default: str = "") -> str:
        if isinstance(meta, dict):
            return str(meta.get(field) or default)
        return str(getattr(meta, field, None) or default)

    @staticmethod
    def _parse_umo(value: Any) -> tuple[str, str, str] | None:
        if not isinstance(value, str) or not value or len(value) > 512:
            return None
        if any(ord(char) < 32 for char in value):
            return None
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        platform_id, message_type, session_id = parts
        if message_type not in MESSAGE_TYPES:
            return None
        return platform_id, message_type, session_id

    @staticmethod
    def _normalize_username(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        username = value.strip().lstrip("@")
        if not USERNAME_PATTERN.fullmatch(username):
            return None
        return username

    @staticmethod
    def _subscription_row(username: str, author: dict, sub: dict) -> dict:
        return {
            "username": username,
            "screen_name": str(author.get("screen_name") or username),
            "enabled": bool(sub.get("status", True)),
            "r18": bool(sub.get("r18", False)),
            "media_only": bool(sub.get("media", False)),
            "since_id": str(author.get("since_id") or ""),
        }

    def _platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        instances = getattr(manager, "platform_insts", None)
        if instances is None:
            get_insts = getattr(manager, "get_insts", None)
            instances = get_insts() if callable(get_insts) else []
        return list(instances or [])

    def _aiocqhttp_platforms(self) -> list[Any]:
        result = []
        for platform in self._platform_instances():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if self._meta_value(meta, "name").casefold() == "aiocqhttp":
                result.append(platform)
        return result

    async def _fetch_platform_groups(self, platform: Any) -> dict:
        meta = platform.meta()
        platform_id = self._meta_value(meta, "id")
        platform_label = self._meta_value(meta, "name", "aiocqhttp")
        result = {
            "platform_id": platform_id,
            "platform_name": platform_label,
            "available": False,
            "error": None,
            "groups": [],
        }
        if not platform_id:
            result["error"] = "平台实例缺少 ID"
            return result

        try:
            client = platform.get_client()
            response = await asyncio.wait_for(
                client.call_action(action="get_group_list"),
                timeout=GROUP_LIST_TIMEOUT_SECONDS,
            )
            if isinstance(response, dict):
                groups = response.get("data", response.get("groups", []))
            else:
                groups = response
            if not isinstance(groups, (list, tuple)):
                raise ValueError("get_group_list 返回格式不正确")

            normalized_groups = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                normalized_groups.append(
                    {
                        "group_id": group_id,
                        "group_name": str(group.get("group_name") or "").strip(),
                    }
                )
            result["groups"] = normalized_groups
            result["available"] = True
        except Exception as exc:
            result["error"] = str(exc) or exc.__class__.__name__
            logger.warning(f"获取 QQ 群列表失败 ({platform_id}): {exc}")
        return result

    async def _load_group_sources(self) -> list[dict]:
        platforms = self._aiocqhttp_platforms()
        if not platforms:
            return []
        return list(
            await asyncio.gather(
                *(self._fetch_platform_groups(platform) for platform in platforms)
            )
        )

    @staticmethod
    def _session_label(message_type: str, session_id: str) -> str:
        labels = {
            "FriendMessage": "私聊",
            "OtherMessage": "其他会话",
        }
        return f"{labels.get(message_type, message_type)} {session_id}"

    async def _build_overview(self) -> dict:
        subs, group_sources = await asyncio.gather(
            self.plugin._get_subscriptions_snapshot(),
            self._load_group_sources(),
        )

        session_subscriptions: dict[str, list[dict]] = {}
        total_relations = 0
        active_relations = 0
        for username, author in subs.items():
            if not isinstance(author, dict):
                continue
            subscribers = author.get("subscribers", {})
            if not isinstance(subscribers, dict):
                continue
            for umo, sub in subscribers.items():
                if not isinstance(umo, str) or not isinstance(sub, dict):
                    continue
                row = self._subscription_row(str(username), author, sub)
                session_subscriptions.setdefault(umo, []).append(row)
                total_relations += 1
                if row["enabled"]:
                    active_relations += 1

        groups_by_umo: dict[str, dict] = {}
        for source in group_sources:
            platform_id = source["platform_id"]
            for group in source["groups"]:
                umo = f"{platform_id}:GroupMessage:{group['group_id']}"
                groups_by_umo[umo] = {
                    "umo": umo,
                    "platform_id": platform_id,
                    "group_id": group["group_id"],
                    "group_name": group["group_name"] or f"群聊 {group['group_id']}",
                    "available": True,
                    "subscriptions": [],
                }

        other_sessions = []
        for umo, rows in session_subscriptions.items():
            parsed = self._parse_umo(umo)
            if parsed is None:
                other_sessions.append(
                    {
                        "umo": umo,
                        "platform_id": "",
                        "session_id": umo,
                        "session_type": "Unknown",
                        "session_name": umo,
                        "subscriptions": rows,
                    }
                )
                continue
            platform_id, message_type, session_id = parsed
            if message_type == "GroupMessage":
                group = groups_by_umo.setdefault(
                    umo,
                    {
                        "umo": umo,
                        "platform_id": platform_id,
                        "group_id": session_id,
                        "group_name": f"群聊 {session_id}",
                        "available": False,
                        "subscriptions": [],
                    },
                )
                group["subscriptions"] = rows
            else:
                other_sessions.append(
                    {
                        "umo": umo,
                        "platform_id": platform_id,
                        "session_id": session_id,
                        "session_type": message_type,
                        "session_name": self._session_label(message_type, session_id),
                        "subscriptions": rows,
                    }
                )

        groups = list(groups_by_umo.values())
        for group in groups:
            group["subscriptions"].sort(key=lambda row: row["username"].casefold())
        for session in other_sessions:
            session["subscriptions"].sort(
                key=lambda row: row["username"].casefold()
            )
        groups.sort(
            key=lambda group: (
                group["group_name"].casefold(),
                group["group_id"],
                group["platform_id"],
            )
        )
        other_sessions.sort(
            key=lambda session: session["session_name"].casefold()
        )

        provider_name = str(getattr(self.plugin, "data_provider", "nitter"))
        return {
            "provider": {
                "name": provider_name,
                "ready": bool(getattr(self.plugin, "_provider_ready", False)),
            },
            "polling": {
                "running": bool(getattr(self.plugin, "_running", False)),
                "interval_minutes": int(getattr(self.plugin, "poll_interval", 5)),
            },
            "totals": {
                "groups": len(groups),
                "sessions": len(session_subscriptions),
                "authors": len(subs),
                "subscriptions": total_relations,
                "active": active_relations,
            },
            "group_sources": [
                {
                    "platform_id": source["platform_id"],
                    "available": source["available"],
                    "error": source["error"],
                    "group_count": len(source["groups"]),
                }
                for source in group_sources
            ],
            "groups": groups,
            "other_sessions": other_sessions,
        }

    @staticmethod
    async def _payload() -> dict | None:
        payload = await request.json(default={})
        return payload if isinstance(payload, dict) else None

    async def _validate_live_group(
        self,
        parsed_umo: tuple[str, str, str],
    ) -> tuple[str, int] | None:
        platform_id, message_type, session_id = parsed_umo
        if message_type != "GroupMessage":
            return "只能为群聊新增订阅", 400

        platform = next(
            (
                item
                for item in self._aiocqhttp_platforms()
                if self._meta_value(item.meta(), "id") == platform_id
            ),
            None,
        )
        if platform is None:
            return "未找到对应的 aiocqhttp 平台实例", 400

        source = await self._fetch_platform_groups(platform)
        if not source["available"]:
            return "暂时无法确认机器人所在群列表", 503
        if not any(group["group_id"] == session_id for group in source["groups"]):
            return "机器人当前不在这个群聊中", 404
        return None

    async def overview(self):
        try:
            return json_response(await self._build_overview())
        except Exception as exc:
            logger.exception(f"生成 Twitter 订阅概览失败: {exc}")
            return error_response("读取订阅数据失败", status_code=500)

    async def save_poll_interval(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        minutes = payload.get("minutes")
        if type(minutes) is not int or minutes < 1:
            return error_response("轮询间隔必须是不少于 1 的整数", status_code=400)

        try:
            await self.plugin._set_poll_interval(minutes)
        except Exception as exc:
            logger.error(f"保存 Twitter 轮询间隔失败: {exc}")
            return error_response("轮询间隔保存失败，原配置已恢复", status_code=500)
        return json_response({"saved": True, "minutes": minutes})

    async def add_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        parsed_umo = self._parse_umo(payload.get("umo"))
        username = self._normalize_username(payload.get("username"))
        if parsed_umo is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        if username is None:
            return error_response("推主用户名格式不正确", status_code=400)

        for field in ("r18", "media_only"):
            if field in payload and type(payload[field]) is not bool:
                return error_response(f"{field} 必须是布尔值", status_code=400)

        group_error = await self._validate_live_group(parsed_umo)
        if group_error:
            message, status_code = group_error
            return error_response(message, status_code=status_code)

        try:
            result = await self.plugin._add_subscription(
                payload["umo"],
                username,
                r18=payload.get("r18", False),
                media_only=payload.get("media_only", False),
                reject_duplicate=True,
            )
        except Exception as exc:
            logger.warning(f"WebUI 新增 @{username} 订阅失败: {exc}")
            return error_response("获取推主资料或时间线失败", status_code=502)

        reason = result.get("reason")
        if reason == "duplicate":
            return error_response("当前群已订阅这个推主", status_code=409)
        if reason == "not_found":
            return error_response("未找到这个推主", status_code=404)
        if reason == "provider_unavailable":
            return error_response("Twitter 数据源当前不可用", status_code=503)
        return json_response(
            {
                "saved": True,
                "username": result["username"],
                "screen_name": result["screen_name"],
            }
        )

    async def update_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        if self._parse_umo(payload.get("umo")) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        username = self._normalize_username(payload.get("username"))
        if username is None:
            return error_response("推主用户名格式不正确", status_code=400)

        changes = {}
        for field in ("enabled", "r18", "media_only"):
            if field not in payload:
                continue
            if type(payload[field]) is not bool:
                return error_response(f"{field} 必须是布尔值", status_code=400)
            changes[field] = payload[field]
        if not changes:
            return error_response("没有可保存的订阅选项", status_code=400)

        result = await self.plugin._update_subscription(
            payload["umo"],
            username,
            changes,
        )
        if not result["ok"]:
            return error_response("没有找到这条订阅", status_code=404)
        return json_response({"saved": True, "username": result["username"]})

    async def update_group_status(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        parsed_umo = self._parse_umo(payload.get("umo"))
        if parsed_umo is None or parsed_umo[1] != "GroupMessage":
            return error_response("群聊 UMO 格式不正确", status_code=400)
        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            return error_response("enabled 必须是布尔值", status_code=400)

        count = await self.plugin._set_session_subscriptions_status(
            payload["umo"],
            enabled,
        )
        if not count:
            return error_response("这个群当前没有订阅", status_code=404)
        return json_response({"saved": True, "updated": count})

    async def remove_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        if self._parse_umo(payload.get("umo")) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        username = self._normalize_username(payload.get("username"))
        if username is None:
            return error_response("推主用户名格式不正确", status_code=400)

        result = await self.plugin._remove_subscription(payload["umo"], username)
        if not result["ok"]:
            return error_response("没有找到这条订阅", status_code=404)
        return json_response(
            {
                "saved": True,
                "username": result["username"],
                "author_removed": result["author_removed"],
            }
        )
