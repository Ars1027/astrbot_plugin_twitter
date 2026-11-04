"""Nitter 失败契约及游标保护，不访问真实镜像或发送消息。"""

import copy
import types

import httpx
import pytest

from test_fxtwitter_api import api_module as api_module
from test_provider_initialization import plugin_module as plugin_module


DETAIL = '''<div class="main-tweet">
<a class="fullname">Tester</a><a class="username">@tester</a>
<div class="tweet-content media-body">正文</div><div class="nsfw"></div>
<a class="still-image" href="/pic/main.jpg"><img src="/pic/main.jpg"></a>
<div class="attachment"><video poster="/pic/poster.jpg"><source src="/video.mp4"></video></div>
<div class="quote"><a class="fullname">Quoted</a><a class="username">@quoted</a>
<a class="quote-link" href="/quoted/status/99"></a><div class="quote-text">引用正文</div>
<a class="still-image" href="/pic/quote.jpg"><img src="/pic/quote.jpg"></a></div>
</div>'''
TIMELINE = '<div class="timeline-item"><a class="tweet-link" href="/tester/status/101"></a></div>'


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [503, 429, "timeout", "parse"])
async def test_nitter_timeline_failure_raises_compatible_error(api_module, monkeypatch, failure):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(failure if isinstance(failure, int) else 200, text=TIMELINE)

    if failure == "parse":
        def fail_parse(*_args, **_kwargs):
            raise ValueError("broken parser")
        monkeypatch.setattr(api, "_parse_timeline_items", fail_parse)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        with pytest.raises(api_module.FxTwitterTimelineError):
            await api.get_user_timeline_items("tester", "100")
    assert api_module.FxTwitterTimelineError is api_module.TwitterTimelineError


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected", [("<div class='timeline'></div>", []), (TIMELINE, ["101"])])
async def test_nitter_timeline_success_and_empty_remain_valid(api_module, body, expected):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as client:
        api._client = client
        assert await api.get_user_newtimeline("tester", "100") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_parser", ["_extract_avatar", "_extract_date", "_contains_live_stream"])
async def test_nitter_required_parse_failure_is_not_success(api_module, monkeypatch, failed_parser):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")

    def fail(*_args, **_kwargs):
        raise ValueError("broken parser")

    monkeypatch.setattr(api, failed_parser, fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=DETAIL))) as client:
        api._client = client
        assert (await api.get_tweet("tester", "101"))["status"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_parser", [None, "_extract_images", "_extract_videos", "_extract_video_previews", "_attach_nitter_article"])
async def test_nitter_optional_failure_preserves_body_and_other_media(api_module, monkeypatch, failed_parser):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    if failed_parser:
        def fail(*_args, **_kwargs):
            raise ValueError("optional parser failure")
        monkeypatch.setattr(api, failed_parser, fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=DETAIL))) as client:
        api._client = client
        result = await api.get_tweet("tester", "101")
    assert result["status"] is True
    assert result["text"] == "正文"
    assert result["is_r18"] is True
    assert result["quote"]["text"] == "引用正文"
    if failed_parser != "_extract_images":
        assert result["images"] == ["https://nitter.test/pic/main.jpg"]
        assert result["quote"]["images"] == ["https://nitter.test/pic/quote.jpg"]
    if failed_parser != "_extract_videos":
        assert result["videos"] == ["https://nitter.test/video.mp4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeline", "detail"])
async def test_real_nitter_failure_does_not_send_or_commit_cursor(api_module, plugin_module, monkeypatch, failure):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    store = {"tester": {"since_id": "100", "subscribers": {"session": {"status": True}}}}

    async def get_kv(_key, _default):
        return copy.deepcopy(store)

    async def put_kv(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    async def send(*_args, **_kwargs):
        pytest.fail("详情或时间线失败时不得发送")

    if failure == "detail":
        def fail_parse(*_args, **_kwargs):
            raise ValueError("broken parser")
        monkeypatch.setattr(api, "_extract_avatar", fail_parse)
    subscriptions = plugin_module.SubscriptionService(get_kv, put_kv, api, lambda: True)
    polling = plugin_module.PollingService(
        api, subscriptions, types.SimpleNamespace(push_to_subscribers=send, collective_enabled=False),
        plugin_module.PollingSettings(include_retweets=True, data_provider="nitter", custom_nitter_url="", website_list=()),
    )

    def respond(request):
        if request.url.path == "/tester":
            return httpx.Response(503 if failure == "timeline" else 200, text=TIMELINE)
        return httpx.Response(200, text=DETAIL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        assert await polling.check_user("tester", copy.deepcopy(store["tester"])) is False
    assert store["tester"]["since_id"] == "100"
    if failure == "detail":
        assert store["tester"]["pending_tweet_items"][0]["tweet_id"] == "101"


@pytest.mark.asyncio
async def test_nitter_timeline_failure_commands_do_not_create_empty_cursor(api_module, plugin_module, monkeypatch):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    plugin = plugin_module.TwitterPlugin.__new__(plugin_module.TwitterPlugin)
    plugin._provider_ready = True
    plugin.twitter_api = api
    monkeypatch.setattr(plugin_module, "FxTwitterTimelineError", api_module.TwitterTimelineError)

    async def user_info(_username):
        return {"status": True, "screen_name": "Tester", "user_name": "tester"}

    async def get_kv(_key, _default):
        return {}

    async def put_kv(*_args):
        pytest.fail("初始化时间线失败时不得创建订阅")

    monkeypatch.setattr(api, "get_user_info", user_info)
    plugin.subscription_service = plugin_module.SubscriptionService(get_kv, put_kv, api, lambda: True)
    event = types.SimpleNamespace(
        unified_msg_origin="session", plain_result=lambda text: text,
        message_str="/推特关注 tester",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        api._client = client
        followed = [result async for result in plugin.follow_twitter(event, "tester")]
        tested = [result async for result in plugin.test_tweet(event, "tester")]
    assert followed == ["获取 @tester 时间线失败，请稍后重试"]
    assert tested[-1] == "获取 @tester 时间线失败，请稍后重试"
