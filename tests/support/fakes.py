"""AstrBot component doubles and reusable platform fakes."""

import sys
import types
from pathlib import Path

class SilentLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Component:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plain(_Component):
    def __init__(self, text):
        super().__init__(text=text)


class Image(_Component):
    @staticmethod
    def fromURL(url):
        return Image(file=url)

    @staticmethod
    def fromFileSystem(path):
        return Image(file=path)

    @staticmethod
    def fromBytes(data):
        return Image(data=data)


class Video(_Component):
    @staticmethod
    def fromURL(url):
        return Video(file=url)


class Node(_Component):
    pass


class Nodes(_Component):
    def __init__(self, nodes):
        super().__init__(nodes=nodes)


class MessageChain(_Component):
    pass


class FakeTwitterAPI:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.provider = kwargs.get("provider", "nitter")
        self.nitter_url = kwargs.get("nitter_url", "")
        self.provider_ready = False
        self.fx_checks = 0
        self.nitter_checks = 0
        self.closed = False
        self.__class__.instances.append(self)

    @property
    def is_ready(self):
        if self.provider == "fxtwitter":
            return self.provider_ready
        return bool(self.nitter_url)

    async def check_fxtwitter_available(self):
        self.fx_checks += 1
        self.provider_ready = True
        return True

    async def check_website_available(self, websites):
        self.nitter_checks += 1
        self.nitter_url = websites[0] if websites else "https://nitter.test"
        self.provider_ready = True
        return self.nitter_url

    async def close(self):
        self.closed = True


class FakeFxTwitterTimelineError(RuntimeError):
    pass


class MediaImage(_Component):
    @staticmethod
    def fromURL(url):
        if not url.startswith(("http://", "https://")):
            raise ValueError("not a valid URL")
        return MediaImage(file=url)

    @staticmethod
    def fromFileSystem(path):
        return MediaImage(file=Path(path).resolve().as_uri(), path=str(Path(path).resolve()))

    @staticmethod
    def fromBytes(data):
        return MediaImage(data=data)


class MediaNode(_Component):
    def __init__(self, content, name):
        super().__init__(content=content, name=name)


class MediaMessageChain(_Component):
    def __init__(self, chain):
        super().__init__(chain=chain)


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def __getattr__(self, name):
        def log(message, *_args, **_kwargs):
            self.messages.append((name, str(message)))

        return log


class FakeRequest:
    def __init__(self):
        self.payload = {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


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
        self.telegram_groups = {}
        self.subscription_service = types.SimpleNamespace(
            get_telegram_groups=self.get_telegram_groups,
            register_telegram_group=self.register_telegram_group,
        )
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

    async def get_telegram_groups(self):
        return self.telegram_groups

    async def register_telegram_group(self, platform_id, group):
        umo = f"{platform_id}:GroupMessage:{group['group_id']}"
        created = umo not in self.telegram_groups
        self.telegram_groups[umo] = dict(group)
        return created

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


TG_GROUP = "my-bot:GroupMessage:-100123"
TG_TOPIC = "my-bot:GroupMessage:-100123#42"
TG_PRIVATE = "my-bot:FriendMessage:12345"


class TelegramClient:
    id = 9876

    def __init__(self, *, status="member", error=None, is_member=False, can_send_messages=False):
        self.status = status
        self.error = error
        self.is_member = is_member
        self.can_send_messages = can_send_messages
        self.calls = []

    async def get_chat(self, *, chat_id):
        self.calls.append(("chat", chat_id))
        if self.error:
            raise self.error
        return types.SimpleNamespace(type="supergroup", title="Telegram 测试群")

    async def get_chat_member(self, *, chat_id, user_id):
        self.calls.append(("member", chat_id, user_id))
        return types.SimpleNamespace(
            status=self.status, is_member=self.is_member,
            can_send_messages=self.can_send_messages,
        )


def telegram_platform(**options):
    platform = FakePlatform("my-bot", name="telegram")
    platform.client = TelegramClient(**options)
    return platform


def make_event(umo, message):
    return types.SimpleNamespace(
        unified_msg_origin=umo, message_str=message,
        get_platform_name=lambda: "telegram",
        plain_result=lambda text: text, chain_result=lambda chain: chain,
    )


def clock_for(plugin, monkeypatch):
    module = sys.modules[type(plugin.delivery_service.send_backoff).__module__]
    clock = types.SimpleNamespace(now=100.0, sleeps=[], logs=[])

    async def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module, "asyncio", types.SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(module.logger, "warning", clock.logs.append)
    return clock, module


def _delivery_contract(plugin_module):
    return sys.modules[
        f"{plugin_module.__package__}.services.tweet_delivery_service"
    ]


def _message_settings(plugin_module, **overrides):
    values = {
        "no_text": False,
        "send_media_separately": True,
        "include_tweet_link": False,
        "text_render_mode": "screenshot",
        "screenshot_theme": "dark",
        "video_max_size_mb": 256,
        "translate_enabled": False,
        "translate_target_lang": "简体中文",
        "translate_provider_id": "",
        "translate_custom_prompt_enabled": False,
        "translate_custom_prompt": "",
        "pre_download_media": True,
        "proxy": "http://127.0.0.1:7890",
    }
    values.update(overrides)
    return plugin_module.TweetMessageSettings(**values)


def _delivery_settings(plugin_module, **overrides):
    values = {
        "use_node": False,
        "collective_forward": False,
        "collective_max_authors": 5,
        "deduplicate_retweets": False,
    }
    values.update(overrides)
    return plugin_module.TweetDeliverySettings(**values)
