"""Load production modules under isolated, monkeypatch-managed test packages."""

import importlib.util
import sys
import types
from pathlib import Path

from . import fakes

ROOT = Path(__file__).resolve().parents[2]


def _namespace(monkeypatch, name, path):
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    monkeypatch.setitem(sys.modules, name, package)
    return package


def _load(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    monkeypatch.setitem(sys.modules, spec.name, module)
    try:
        spec.loader.exec_module(module)
    finally:
        # Imports performed by production code also belong to this fixture.
        # Reinsert through monkeypatch so teardown removes them as well.
        prefix = name.split('.')[0] + '.'
        for imported in set(sys.modules) - before - {spec.name}:
            if imported.startswith(prefix):
                value = sys.modules.pop(imported)
                monkeypatch.setitem(sys.modules, imported, value)
    return module


def _package(monkeypatch, name):
    for existing in list(sys.modules):
        if existing == name or existing.startswith(name + '.'):
            monkeypatch.delitem(sys.modules, existing)
    _namespace(monkeypatch, name, ROOT)


def _api(monkeypatch, logger):
    astrbot = types.ModuleType('astrbot')
    api = types.ModuleType('astrbot.api')
    api.logger = logger
    monkeypatch.setitem(sys.modules, 'astrbot', astrbot)
    monkeypatch.setitem(sys.modules, 'astrbot.api', api)
    return api


def _decorator(*_args, **_kwargs):
    return lambda func: func


def load_main_module(monkeypatch, *, media=False):
    package_name = 'twitter_media_test_package' if media else 'twitter_provider_test_package'
    _package(monkeypatch, package_name)
    api = _api(monkeypatch, fakes.SilentLogger())
    api.AstrBotConfig = dict
    event = types.ModuleType('astrbot.api.event')
    components = types.ModuleType('astrbot.api.message_components')
    star = types.ModuleType('astrbot.api.star')
    event.AstrMessageEvent = object
    event.MessageChain = fakes.MediaMessageChain if media else fakes.MessageChain
    event.filter = types.SimpleNamespace(
        command=_decorator, event_message_type=_decorator,
        permission_type=_decorator, llm_tool=_decorator,
        on_astrbot_loaded=_decorator,
        EventMessageType=types.SimpleNamespace(ALL='all'),
        PermissionType=types.SimpleNamespace(ADMIN='admin'),
    )
    for name in ('Plain', 'Image', 'Video', 'Node', 'Nodes'):
        setattr(components, name, getattr(fakes, 'Media' + name if media and name in ('Image', 'Node') else name))

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    star.StarTools = types.SimpleNamespace()
    for name, value in (('event', event), ('message_components', components), ('star', star)):
        monkeypatch.setitem(sys.modules, 'astrbot.api.' + name, value)
    twitter_api = types.ModuleType(package_name + '.twitter_api')
    twitter_api.DATA_PROVIDER_NITTER = 'nitter'
    twitter_api.DATA_PROVIDER_FXTWITTER = 'fxtwitter'
    twitter_api.DATA_PROVIDER_OPTIONS = ('nitter', 'fxtwitter')
    twitter_api.DEFAULT_FXTWITTER_API_BASE = 'https://api.fxtwitter.com'
    twitter_api.FxTwitterTimelineError = RuntimeError if media else fakes.FakeFxTwitterTimelineError
    twitter_api.TwitterAPI = object if media else fakes.FakeTwitterAPI
    twitter_api.WEBSITE_LIST = [] if media else ['https://nitter.test']
    twitter_api.get_next_website = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, twitter_api.__name__, twitter_api)
    return _load(monkeypatch, package_name + '.main', ROOT / 'main.py')


def load_api_module(monkeypatch):
    package_name = 'twitter_api_test_package'
    _package(monkeypatch, package_name)
    _api(monkeypatch, fakes.RecordingLogger())
    return _load(monkeypatch, package_name + '.twitter_api', ROOT / 'twitter_api.py')


def load_webui_module(monkeypatch):
    package_name = 'twitter_webui_test_package'
    _package(monkeypatch, package_name)
    _api(monkeypatch, fakes.SilentLogger())
    web = types.ModuleType('astrbot.api.web')
    request = fakes.FakeRequest()
    web.request = request
    web.json_response = lambda data: (data, 200)
    web.error_response = lambda message, status_code=400: ({'message': message}, status_code)
    monkeypatch.setitem(sys.modules, 'astrbot.api.web', web)
    # Only platform_support is needed; do not execute services/__init__.py,
    # which eagerly imports message components from the optional AstrBot SDK.
    _namespace(monkeypatch, package_name + '.services', ROOT / 'services')
    module = _load(monkeypatch, package_name + '.twitter_webui', ROOT / 'twitter_webui.py')
    module.fake_request = request
    return module
