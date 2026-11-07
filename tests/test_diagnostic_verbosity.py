import asyncio
import sys
import types

import httpx
import pytest
from test_fxtwitter_api import _load_twitter_api_module
from test_provider_initialization import Image, Plain
from test_telegram_albums import albums as albums  # noqa: PLC0414
from test_telegram_support import TG_GROUP
from test_telegram_support import plugin_env as plugin_env  # noqa: PLC0414


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_empty_author_check_has_one_summary_unless_details_enabled(plugin_env, monkeypatch, verbose):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    api_module = _load_twitter_api_module()
    api = api_module.TwitterAPI(provider="fxtwitter")
    api.provider_ready = True
    api.detailed_diagnostics = verbose
    polling = plugin.polling_service
    polling.twitter_api = api
    polling.detailed_diagnostics = verbose
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_GROUP: {}}}}
    logs = []
    monkeypatch.setattr(sys.modules[type(polling).__module__].logger, "debug", logs.append)
    monkeypatch.setattr(api_module.logger, "debug", logs.append)

    def handler(_request):
        return httpx.Response(200, json={"code": 200, "results": [
            {"type": "status", "id": "100", "author": {"screen_name": "tester"}},
        ]})

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await polling.check_user("tester", data["twitter_subs"]["tester"])
    finally:
        await api.close()
    summaries = [line for line in logs if "poll.author.summary" in line]
    assert len(summaries) == 1
    assert 'since="100"' in summaries[0] and "new=0" in summaries[0]
    assert 'status="no_updates"' in summaries[0]
    if not verbose:
        assert logs == summaries
    else:
        for event in ("poll.author.start", "poll.retry_window", "fx.request.start", "fx.request.response",
                      "timeline.page.start", "timeline.page.result", "timeline.complete", "poll.author.end"):
            assert any(f"[twitter:{event}]" in line for line in logs)


@pytest.mark.parametrize("config", [{}, {"basic": {"twitter_detailed_diagnostics": True}},
                                  {"twitter_detailed_diagnostics": True},
                                  {"basic": {"twitter_detailed_diagnostics": False}, "twitter_detailed_diagnostics": True}])
def test_diagnostics_setting_defaults_legacy_precedence_and_instance_isolation(plugin_env, config):
    module, make, _data, _calls, _fail = plugin_env
    other = make()
    plugin = module.TwitterPlugin(other.context, config)
    expected = config.get("basic", {}).get("twitter_detailed_diagnostics", config.get("twitter_detailed_diagnostics", False))
    for owner in (plugin.twitter_api, plugin.subscription_service, plugin.message_service,
                  plugin.delivery_service, plugin.delivery_service.telegram_albums, plugin.polling_service):
        assert owner.detailed_diagnostics is expected
    assert not other.polling_service.detailed_diagnostics
    assert not other.twitter_api.detailed_diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failures_and_interruptions_keep_author_summary(plugin_env, monkeypatch, cancelled):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_GROUP: {}}}}
    module = sys.modules[type(plugin.polling_service).__module__]
    logs, errors = [], []
    monkeypatch.setattr(module.logger, "debug", logs.append)
    monkeypatch.setattr(module.logger, "error", errors.append)

    async def timeline(*_args):
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("request failed")

    plugin.twitter_api.get_user_timeline_items = timeline
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await plugin.polling_service.check_user("tester", data["twitter_subs"]["tester"])
    else:
        assert not await plugin.polling_service.check_user("tester", data["twitter_subs"]["tester"])
        assert errors
    assert len(logs) == 1 and "poll.author.summary" in logs[0]
    assert "new=null" in logs[0] and (('status="interrupted"' if cancelled else 'status="failed"') in logs[0])
    assert data["twitter_subs"]["tester"]["since_id"] == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_album_and_platform_stages_follow_switch_without_changing_delivery(albums, monkeypatch, verbose):
    plugin, _data, ordinary, sent, _converted, _client, chain = albums
    delivery = plugin.delivery_service
    delivery.detailed_diagnostics = delivery.telegram_albums.detailed_diagnostics = verbose
    logs = []
    monkeypatch.setattr(sys.modules[type(delivery).__module__].logger, "debug", logs.append)
    images = [Image(file="a.png"), Image(file="b.png")]
    assert await delivery.send_telegram_tweet(TG_GROUP, chain(images), "tester", {"tweet_id": "101"})
    await delivery._send_message_checked(TG_GROUP, types.SimpleNamespace(chain=[Plain("body")]))
    assert len(sent) == len(ordinary) == 1
    assert any("tg.upload.start" in line for line in logs) is verbose
    assert any("platform.send.start" in line for line in logs) is verbose
    if not verbose:
        assert not logs


@pytest.mark.asyncio
async def test_new_tweet_results_and_cursor_still_logged_by_default(plugin_env, monkeypatch):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    logs = []
    monkeypatch.setattr(sys.modules[type(plugin.polling_service).__module__].logger, "debug", logs.append)
    data["twitter_subs"] = {"tester": {"since_id": "100", "subscribers": {TG_GROUP: {}}}}

    async def timeline(*_args):
        return [{"tweet_id": "101", "username": "tester", "is_retweet": False}]

    async def detail(*_args):
        return {"tweet_id": "101", "username": "tester", "text": "body"}

    async def build(*_args, **_kwargs):
        return [Plain("body")]

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = detail
    plugin.message_service.build_message_chain = build
    assert await plugin.polling_service.check_user("tester", data["twitter_subs"]["tester"])
    for event in ("poll.tweet.start", "poll.tweet.result", "cursor.commit.result", "poll.author.summary"):
        assert any(f"[twitter:{event}]" in line for line in logs)
    summary = next(line for line in logs if "poll.author.summary" in line)
    assert 'since="100"' in summary and "new=1" in summary and "handled=1" in summary
    assert data["twitter_subs"]["tester"]["since_id"] == "101"
