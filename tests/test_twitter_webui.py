import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class FakeRequest:
    def __init__(self):
        self.payload = {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


def _load_webui_module():
    module_name = "twitter_webui_test_module"
    sys.modules.pop(module_name, None)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    web = types.ModuleType("astrbot.api.web")
    fake_request = FakeRequest()

    api.logger = _Logger()
    web.request = fake_request
    web.json_response = lambda data: (data, 200)
    web.error_response = lambda message, status_code=400: (
        {"message": message},
        status_code,
    )
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.web": web,
        }
    )

    spec = importlib.util.spec_from_file_location(module_name, ROOT / "twitter_webui.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.fake_request = fake_request
    return module


class Meta:
    def __init__(self, platform_id, name="aiocqhttp"):
        self.id = platform_id
        self.name = name


class FakeClient:
    def __init__(self, groups=None, error=None):
        self.groups = groups or []
        self.error = error
        self.calls = 0

    async def call_action(self, *, action):
        assert action == "get_group_list"
        self.calls += 1
        if self.error:
            raise self.error
        return self.groups


class FakePlatform:
    def __init__(self, platform_id, groups=None, error=None, name="aiocqhttp"):
        self._meta = Meta(platform_id, name)
        self.client = FakeClient(groups, error)

    def meta(self):
        return self._meta

    def get_client(self):
        return self.client


class FakeContext:
    def __init__(self, platforms=()):
        self.routes = []
        self.platform_manager = types.SimpleNamespace(platform_insts=list(platforms))

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


class FakePlugin:
    def __init__(self, subs=None):
        self.subs = subs or {}
        self.data_provider = "fxtwitter"
        self._provider_ready = True
        self._running = True
        self.poll_interval = 5
        self.interval_error = None
        self.last_add = None
        self.last_update = None
        self.last_group_status = None
        self.last_remove = None
        self.add_result = {
            "ok": True,
            "username": "tester",
            "screen_name": "Tester",
        }
        self.update_result = {"ok": True, "username": "tester"}
        self.group_status_count = 1
        self.remove_result = {
            "ok": True,
            "username": "tester",
            "author_removed": False,
        }

    async def _get_subscriptions_snapshot(self):
        return self.subs

    async def _set_poll_interval(self, minutes):
        if self.interval_error:
            raise self.interval_error
        self.poll_interval = minutes

    async def _add_subscription(self, umo, username, **options):
        self.last_add = (umo, username, options)
        return self.add_result

    async def _update_subscription(self, umo, username, changes):
        self.last_update = (umo, username, changes)
        return self.update_result

    async def _set_session_subscriptions_status(self, umo, enabled):
        self.last_group_status = (umo, enabled)
        return self.group_status_count

    async def _remove_subscription(self, umo, username):
        self.last_remove = (umo, username)
        return self.remove_result


@pytest.fixture
def webui_module():
    return _load_webui_module()


@pytest.mark.asyncio
async def test_registers_routes_and_builds_multi_bot_overview(webui_module):
    platform_a = FakePlatform(
        "bot-a",
        [
            {"group_id": 100, "group_name": "Alpha Group"},
            {"group_id": 101, "group_name": "Empty Group"},
        ],
    )
    platform_b = FakePlatform(
        "bot-b",
        [{"group_id": 200, "group_name": "Beta Group"}],
    )
    subs = {
        "alice": {
            "screen_name": "Alice",
            "since_id": "10",
            "subscribers": {
                "bot-a:GroupMessage:100": {
                    "status": True,
                    "r18": False,
                    "media": True,
                },
                "bot-a:FriendMessage:888": {
                    "status": True,
                    "r18": False,
                    "media": False,
                },
            },
        },
        "bob": {
            "screen_name": "Bob",
            "since_id": "20",
            "subscribers": {
                "bot-b:GroupMessage:200": {
                    "status": False,
                    "r18": True,
                    "media": False,
                }
            },
        },
    }
    context = FakeContext([platform_a, platform_b])
    controller = webui_module.TwitterWebUIController(FakePlugin(subs), context)

    assert len(context.routes) == 7
    assert {route for route, *_rest in context.routes} == {
        "/astrbot_plugin_twitter/overview",
        "/astrbot_plugin_twitter/subscriptions/recent",
        "/astrbot_plugin_twitter/settings/poll-interval",
        "/astrbot_plugin_twitter/subscriptions/add",
        "/astrbot_plugin_twitter/subscriptions/update",
        "/astrbot_plugin_twitter/subscriptions/group-status",
        "/astrbot_plugin_twitter/subscriptions/remove",
    }

    payload, status = await controller.overview()

    assert status == 200
    assert platform_a.client.calls == 1
    assert platform_b.client.calls == 1
    assert payload["totals"] == {
        "groups": 3,
        "sessions": 3,
        "authors": 2,
        "subscriptions": 3,
        "active": 2,
    }
    assert [(group["group_name"], len(group["subscriptions"])) for group in payload["groups"]] == [
        ("Alpha Group", 1),
        ("Beta Group", 1),
        ("Empty Group", 0),
    ]
    assert payload["other_sessions"][0]["session_name"] == "私聊 888"


@pytest.mark.asyncio
async def test_group_list_failure_keeps_existing_group_with_fallback_name(
    webui_module,
):
    platform = FakePlatform("bot-a", error=RuntimeError("offline"))
    subs = {
        "alice": {
            "screen_name": "Alice",
            "since_id": "10",
            "subscribers": {
                "bot-a:GroupMessage:100": {
                    "status": True,
                    "r18": False,
                    "media": False,
                }
            },
        }
    }
    controller = webui_module.TwitterWebUIController(
        FakePlugin(subs),
        FakeContext([platform]),
    )

    payload, status = await controller.overview()

    assert status == 200
    assert payload["group_sources"][0]["available"] is False
    assert payload["groups"][0]["group_name"] == "群聊 100"
    assert payload["groups"][0]["available"] is False


@pytest.mark.asyncio
async def test_add_subscription_validates_group_and_options(webui_module):
    platform = FakePlatform(
        "bot-a",
        [{"group_id": "100", "group_name": "Alpha"}],
    )
    plugin = FakePlugin()
    controller = webui_module.TwitterWebUIController(
        plugin,
        FakeContext([platform]),
    )

    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100",
        "username": "@tester",
        "r18": True,
        "media_only": False,
    }
    payload, status = await controller.add_subscription()

    assert status == 200
    assert payload["saved"] is True
    assert plugin.last_add == (
        "bot-a:GroupMessage:100",
        "tester",
        {"r18": True, "media_only": False, "reject_duplicate": True},
    )

    webui_module.fake_request.payload = {
        "umo": "bot-a:FriendMessage:100",
        "username": "tester",
    }
    _payload, status = await controller.add_subscription()
    assert status == 400

    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100",
        "username": "not-valid!",
    }
    _payload, status = await controller.add_subscription()
    assert status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "umo, unavailable, expected_status",
    [
        ("bot-b:GroupMessage:100", False, 400),
        ("bot-a:GroupMessage:200", False, 404),
        ("bot-a:GroupMessage:100", True, 503),
    ],
)
async def test_add_rechecks_group_membership(
    webui_module, umo, unavailable, expected_status
):
    plugin = FakePlugin()
    platform = FakePlatform(
        "bot-a",
        [{"group_id": "100", "group_name": "Alpha"}],
        error=RuntimeError("offline") if unavailable else None,
    )
    controller = webui_module.TwitterWebUIController(plugin, FakeContext([platform]))
    webui_module.fake_request.payload = {"umo": umo, "username": "tester"}

    payload, status = await controller.add_subscription()

    assert status == expected_status
    assert not payload.get("saved")
    assert plugin.last_add is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason, expected_status",
    [("duplicate", 409), ("not_found", 404), ("provider_unavailable", 503)],
)
async def test_add_reports_rejected_subscription(webui_module, reason, expected_status):
    plugin = FakePlugin()
    plugin.add_result = {"ok": False, "reason": reason}
    platform = FakePlatform("bot-a", [{"group_id": "100", "group_name": "Alpha"}])
    controller = webui_module.TwitterWebUIController(plugin, FakeContext([platform]))
    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100", "username": "tester", "r18": True
    }

    payload, status = await controller.add_subscription()

    assert status == expected_status
    assert not payload.get("saved")
    assert plugin.last_add[2]["reject_duplicate"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("umo, username, expected", [
    ("bot-a:GroupMessage:100", "@Tester", 200),
    ("bot-b:GroupMessage:100", "Tester", 404),
    ("bot-a:GroupMessage:100", "other", 404),
    ("invalid", "Tester", 400),
    ("bot-a:GroupMessage:100", "invalid!", 400),
])
async def test_recent_history_route_respects_subscription_target(
    webui_module, umo, username, expected
):
    records = [{"tweet_id": "123", "text": "最近推送"}]

    async def get_recent(target_umo, target_name):
        if target_umo == "bot-a:GroupMessage:100" and target_name == "Tester":
            return records
        return None

    plugin = FakePlugin()
    plugin.subscription_service = types.SimpleNamespace(get_recent_deliveries=get_recent)
    controller = webui_module.TwitterWebUIController(plugin, FakeContext())
    webui_module.fake_request.payload = {"umo": umo, "username": username}
    payload, status = await controller.recent_deliveries()
    assert status == expected
    if status == 200:
        assert payload == {"items": records}
    else:
        assert "items" not in payload


@pytest.mark.asyncio
async def test_recent_history_storage_error_is_not_an_empty_success(webui_module):
    async def unavailable(*_args):
        raise OSError("storage unavailable")

    plugin = FakePlugin()
    plugin.subscription_service = types.SimpleNamespace(get_recent_deliveries=unavailable)
    controller = webui_module.TwitterWebUIController(plugin, FakeContext())
    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100", "username": "tester"
    }
    payload, status = await controller.recent_deliveries()
    assert status == 500
    assert "items" not in payload


@pytest.mark.asyncio
async def test_update_group_status_remove_and_interval_routes(webui_module):
    plugin = FakePlugin()
    controller = webui_module.TwitterWebUIController(plugin, FakeContext())

    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100",
        "username": "tester",
        "enabled": False,
        "media_only": True,
    }
    _payload, status = await controller.update_subscription()
    assert status == 200
    assert plugin.last_update == (
        "bot-a:GroupMessage:100",
        "tester",
        {"enabled": False, "media_only": True},
    )

    webui_module.fake_request.payload = {
        "umo": "bot-a:GroupMessage:100",
        "enabled": False,
    }
    _payload, status = await controller.update_group_status()
    assert status == 200
    assert plugin.last_group_status == ("bot-a:GroupMessage:100", False)

    webui_module.fake_request.payload = {
        "umo": "bot-a:FriendMessage:888",
        "username": "tester",
    }
    _payload, status = await controller.remove_subscription()
    assert status == 200
    assert plugin.last_remove == ("bot-a:FriendMessage:888", "tester")

    webui_module.fake_request.payload = {"minutes": 8}
    payload, status = await controller.save_poll_interval()
    assert status == 200
    assert payload == {"saved": True, "minutes": 8}
    assert plugin.poll_interval == 8

    for invalid in (True, "8", 0, -1, 1.5):
        webui_module.fake_request.payload = {"minutes": invalid}
        _payload, status = await controller.save_poll_interval()
        assert status == 400

    plugin.interval_error = RuntimeError("save failed")
    webui_module.fake_request.payload = {"minutes": 9}
    _payload, status = await controller.save_poll_interval()
    assert status == 500
