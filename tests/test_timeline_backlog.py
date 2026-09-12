import asyncio
import copy
import importlib.util
import json
import sys
import types

import httpx
import pytest

from test_nitter_timeline import html_page
from test_provider_initialization import ROOT, _load_main_module


@pytest.fixture
def runtime():
    plugin = _load_main_module()
    modules = []
    # 用真实 API/分页/轮询服务覆盖旧测试的隔离桩，仅模拟框架和外部 I/O。
    for name in (
        "twitter_api",
        "services.timeline_backlog_service",
        "services.polling_service",
    ):
        qualified = f"{plugin.__package__}.{name}"
        spec = importlib.util.spec_from_file_location(
            qualified, ROOT / (name.replace(".", "/") + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
        modules.append(module)
    plugin.TwitterTimelineError = modules[0].TwitterTimelineError
    return types.SimpleNamespace(
        plugin=plugin, api=modules[0], backlog=modules[1], polling=modules[2]
    )


@pytest.fixture
def harness(runtime, tmp_path):
    db = tmp_path / "kv.json"
    db.write_text(
        json.dumps(
            {
                "twitter_subs": {
                    "tester": {
                        "since_id": "100",
                        "subscribers": {"group": {"status": True}},
                    }
                }
            }
        )
    )
    env = types.SimpleNamespace(
        calls=[], sent=[], failure="", writes_fail=False, clients=[]
    )

    def read():
        return json.loads(db.read_text())

    async def get_kv(key, default):
        return read().get(key, default)

    async def put_kv(key, value):
        if env.writes_fail:
            raise OSError("disk failure")
        data = read()
        data[key] = value
        db.write_text(json.dumps(data))

    async def send(_umo, message):
        env.sent.append(message)
        return env.failure != "send"

    async def detail(username, tweet_id):
        return dict(
            status=env.failure != "detail",
            tweet_id=tweet_id,
            username=username,
            text="body",
        )

    def create(respond, collective=False, provider="nitter"):
        plugin = runtime.plugin.TwitterPlugin(
            types.SimpleNamespace(send_message=send),
            {
                "twitter_use_node": collective,
                "twitter_collective_forward": collective,
                "twitter_data_provider": provider,
            },
        )
        plugin.get_kv_data, plugin.put_kv_data = get_kv, put_kv
        api = runtime.api.TwitterAPI(
            nitter_url="https://nitter.test", provider=provider
        )
        api.get_tweet = detail
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        env.clients.append(api._client)
        plugin.twitter_api = api
        plugin.subscription_service.twitter_api = api
        settings = runtime.polling.PollingSettings(
            True, provider, "https://nitter.test", ()
        )
        plugin.polling_service = runtime.polling.PollingService(
            api, plugin.subscription_service, plugin.delivery_service, settings
        )
        return plugin

    env.read, env.create, env.put_kv = read, create, put_kv
    return env


def six_pages(env):
    def respond(request):
        page = int(request.url.params.get("cursor", "1"))
        env.calls.append(page)
        if env.failure == f"page{page}":
            return httpx.Response(503)
        return httpx.Response(
            200,
            text=html_page([107 - page], str(page + 1))
            if page < 6
            else html_page([101, 100]),
        )

    return respond


async def check(plugin, env):
    return await plugin.polling_service.check_user(
        "tester", env.read()["twitter_subs"]["tester"]
    )


def author(env):
    return env.read()["twitter_subs"]["tester"]


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_six_pages_reload_then_delivery_failure_and_recovery(harness, collective):
    env = harness
    plugin = env.create(six_pages(env), collective)
    assert await check(plugin, env)
    assert env.calls == [1, 2, 3, 4] and not env.sent
    assert author(env)["since_id"] == "100"
    assert author(env)["timeline_backlog"]["next_cursor"] == "5"
    await plugin.twitter_api.close()

    plugin = env.create(six_pages(env), collective)
    env.failure = "send"
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert env.calls == [1, 2, 3, 4, 5, 6]
    assert author(env)["since_id"] == "100"
    assert author(env).get("processed_tweet_ids", []) == []
    assert len(author(env)["timeline_backlog"]["items"]) == 6
    env.failure = "detail"
    assert not await check(plugin, env)
    assert author(env)["since_id"] == "100"
    env.failure = ""
    await check(plugin, env)
    if collective:
        assert author(env)["since_id"] == "100"
    await plugin.polling_service.flush_pending_collective()
    assert author(env)["since_id"] == "105"
    assert [i["tweet_id"] for i in author(env)["timeline_backlog"]["items"]] == ["106"]
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert author(env)["processed_tweet_ids"] == [
        "101",
        "102",
        "103",
        "104",
        "105",
        "106",
    ]
    assert "timeline_backlog" not in author(env)
    assert env.calls == [1, 2, 3, 4, 5, 6]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_later_http_and_kv_failures_resume_same_page(harness):
    env = harness
    env.failure = "page3"
    plugin = env.create(six_pages(env))
    assert not await check(plugin, env)
    before = author(env)
    assert before["timeline_backlog"]["next_cursor"] == "3"
    env.failure, env.writes_fail = "", True
    assert not await check(plugin, env)
    assert author(env) == before and not env.sent
    env.writes_fail = False
    await check(plugin, env)
    assert env.calls == [1, 2, 3, 3, 3, 4, 5, 6]
    assert author(env)["since_id"] == "105"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_capacity_blocks_without_eviction_and_resumes_after_raising_limit(
    harness,
):
    env = harness
    plugin = env.create(six_pages(env))
    backlog = plugin.polling_service.timeline_backlog
    backlog.MAX_ITEMS = 2
    assert await check(plugin, env)
    saved = author(env)
    assert saved["timeline_backlog"]["phase"] == "blocked"
    assert saved["timeline_backlog"]["next_cursor"] == "3"
    assert await check(plugin, env)
    assert env.calls == [1, 2, 3] and author(env) == saved and not env.sent
    backlog.MAX_ITEMS = 10
    assert await check(plugin, env)
    assert env.calls == [1, 2, 3, 3, 4, 5, 6]
    assert author(env)["since_id"] == "105"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_three_cursor_failures_rescan_preserving_items_and_upper_bound(harness):
    env = harness
    plugin = env.create(six_pages(env))
    env.failure = "page3"
    for _ in range(3):
        assert not await check(plugin, env)
    assert env.calls == [1, 2, 3, 3, 3]
    assert len(author(env)["timeline_backlog"]["items"]) == 2
    env.failure = ""
    assert await check(plugin, env)
    assert env.calls[-4:] == [1, 2, 3, 4]
    await check(plugin, env)
    assert author(env)["processed_tweet_ids"] == ["101", "102", "103", "104", "105"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_source_change_rescans_without_reusing_cursor_or_replacing_context(
    harness,
):
    env = harness
    plugin = env.create(six_pages(env))
    await check(plugin, env)
    before = author(env)["timeline_backlog"]
    plugin.twitter_api.nitter_url = "https://other.test"
    await check(plugin, env)
    after = author(env)["timeline_backlog"]
    assert env.calls == [1, 2, 3, 4, 1, 2, 3, 4]
    assert after["items"] == before["items"] and after["upper_id"] == before["upper_id"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_inflight_page_cannot_attach_after_unsubscribe_and_readd(harness):
    env = harness
    entered, resume = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await resume.wait()
        return httpx.Response(200, text=html_page([101, 100]))

    plugin = env.create(respond)
    task = asyncio.create_task(check(plugin, env))
    await entered.wait()
    await plugin.subscription_service.remove("group", "tester")
    replacement = {"since_id": "200", "subscribers": {"group": {"status": True}}}
    await env.put_kv("twitter_subs", {"tester": replacement})
    resume.set()
    assert await task
    assert author(env) == replacement and not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", [{"version": 99}, {"items": None}, {"upper_id": "broken"}]
)
async def test_corrupt_state_is_preserved_without_network_requests(harness, mutation):
    env = harness
    plugin = env.create(six_pages(env))
    await check(plugin, env)
    data = env.read()["twitter_subs"]
    data["tester"]["timeline_backlog"].update(mutation)
    await env.put_kv("twitter_subs", data)
    assert await check(plugin, env)
    assert env.read()["twitter_subs"] == data and len(env.calls) == 4 and not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_concurrent_commit_rejects_stale_page_snapshot(harness):
    env = harness
    plugin = env.create(six_pages(env))
    await check(plugin, env)
    snapshot = copy.deepcopy(author(env)["timeline_backlog"])
    candidate = copy.deepcopy(snapshot)
    candidate["phase"] = "ready"
    await plugin.subscription_service.save_timeline_backlog(
        "tester", snapshot, candidate
    )
    await plugin.subscription_service.commit_processed_tweets("tester", ["103"], "103")
    assert not await plugin.subscription_service.save_timeline_backlog(
        "tester", snapshot, candidate
    )
    assert author(env)["since_id"] == "103"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,value", [("MAX_BYTES", 1), ("MAX_CURSORS", 1)])
async def test_other_resource_limits_preserve_checkpoint(harness, limit, value):
    env = harness
    plugin = env.create(six_pages(env))
    setattr(plugin.polling_service.timeline_backlog, limit, value)
    assert await check(plugin, env)
    before = author(env)
    assert before["timeline_backlog"]["phase"] == "blocked"
    calls = list(env.calls)
    assert await check(plugin, env)
    assert env.calls == calls and author(env) == before and not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_cancelled_request_leaves_last_saved_page(harness):
    env = harness

    def respond(request):
        if request.url.params:
            raise asyncio.CancelledError
        return httpx.Response(200, text=html_page([103], "2"))

    plugin = env.create(respond)
    with pytest.raises(asyncio.CancelledError):
        await check(plugin, env)
    assert author(env)["timeline_backlog"]["next_cursor"] == "2"
    assert author(env)["timeline_backlog"]["cursor_failures"] == 0
    assert author(env)["since_id"] == "100" and not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_cursor_loop_restarts_next_cycle_without_losing_metadata(harness):
    env = harness
    calls = []

    def respond(request):
        cursor = request.url.params.get("cursor", "")
        calls.append(cursor)
        ids = [104, 103] if len(calls) == 1 else [999, 102]
        return httpx.Response(200, text=html_page(ids, "loop"))

    plugin = env.create(respond)
    assert await check(plugin, env)
    before = author(env)["timeline_backlog"]
    assert before["cursor_failures"] == 3 and calls == ["", "loop"]
    assert {i["tweet_id"] for i in before["items"]} == {"102", "103", "104"}
    assert await check(plugin, env)
    assert calls == ["", "loop", "", "loop"]
    assert author(env)["timeline_backlog"]["items"] == before["items"]
    assert author(env)["since_id"] == "100" and not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_retweet_context_survives_duplicate_page_and_reload(harness):
    env = harness

    def respond(request):
        first = not request.url.params
        html = html_page([102] if first else [102, 101, 100], "2" if first else None)
        header = '<div class="retweet-header">Original context retweeted</div>'
        if first:
            html = html.replace(
                '<a class="tweet-link"', header + '<a class="tweet-link"', 1
            )
        return httpx.Response(200, text=html)

    plugin = env.create(respond)
    plugin.polling_service.timeline_backlog.MAX_PAGES = 1
    assert await check(plugin, env)
    await plugin.twitter_api.close()
    plugin = env.create(respond)
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["101", "102"]
    assert batch.items[1]["is_retweet"]
    assert batch.items[1]["retweeter_screen_name"] == "Original context"
    assert author(env)["since_id"] == "100"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_manual_test_reports_real_nitter_error_without_touching_backlog(harness):
    env = harness
    plugin = env.create(lambda _: httpx.Response(503))
    plugin._provider_ready = True
    event = types.SimpleNamespace(
        unified_msg_origin="group", plain_result=lambda text: text
    )
    before = env.read()
    results = [text async for text in plugin.test_tweet(event, "tester")]
    assert results[-1] == "获取 @tester 时间线失败，请稍后重试"
    assert env.read() == before
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_failed_delivery_commit_keeps_ready_items_for_retry(harness):
    env = harness
    plugin = env.create(lambda _: httpx.Response(200, text=html_page([101, 100])))
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    before = author(env)
    env.writes_fail = True
    assert not await check(plugin, env)
    assert env.sent and author(env) == before
    env.writes_fail = False
    assert await check(plugin, env)
    assert author(env)["since_id"] == "101"
    assert "timeline_backlog" not in author(env)
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_old_retweet_only_page_does_not_freeze_upper_below_anchor(harness):
    env = harness

    def respond(request):
        if request.url.params:
            return httpx.Response(200, text=html_page([104, 103, 100]))
        html = html_page([80], "2").replace(
            '<a class="tweet-link"',
            '<div class="retweet-header">Tester retweeted</div><a class="tweet-link"',
        )
        return httpx.Response(200, text=html)

    plugin = env.create(respond)
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["103", "104"]
    await plugin.twitter_api.close()


def fx_pages(env):
    def respond(request):
        assert request.url.params['count'] == '20'
        page = int(request.url.params.get('cursor', '1'))
        env.calls.append(page)
        if page == 5 and env.failure:
            if env.failure == 'json':
                return httpx.Response(200, text='invalid-json')
            return httpx.Response(int(env.failure))
        ids = [107 - page] if page < 6 else [101, 100]
        return httpx.Response(200, json={
            'code': 200, 'results': [dict(type='status', id=str(i), author={'screen_name': 'tester'}) for i in ids],
            'cursor': {'bottom': str(page + 1) if page < 6 else None},
        })
    return respond


@pytest.mark.asyncio
@pytest.mark.parametrize('collective', [False, True])
async def test_fx_six_pages_persist_across_reload_and_send_failure(harness, collective):
    env = harness
    plugin = env.create(fx_pages(env), collective, 'fxtwitter')
    assert await check(plugin, env)
    assert env.calls == [1, 2, 3, 4] and not env.sent
    assert author(env)['timeline_backlog']['next_cursor'] == '5'
    await plugin.twitter_api.close()
    plugin = env.create(fx_pages(env), collective, 'fxtwitter')
    batch = await plugin.polling_service.timeline_backlog.get_batch('tester')
    assert [i['tweet_id'] for i in batch.items] == ['101', '102', '103', '104', '105', '106']
    assert env.calls == [1, 2, 3, 4, 5, 6] and not env.sent
    env.failure = 'send'
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert author(env)['since_id'] == '100'
    env.failure = ''
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert author(env)['since_id'] == '105'
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert 'timeline_backlog' not in author(env)
    assert author(env)['processed_tweet_ids'] == ['101', '102', '103', '104', '105', '106']
    assert env.calls == [1, 2, 3, 4, 5, 6]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['429', '503', 'json'])
async def test_fx_fifth_page_failure_preserves_progress(harness, monkeypatch, failure):
    env = harness

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    plugin = env.create(fx_pages(env), provider='fxtwitter')
    assert await check(plugin, env)
    env.failure = failure
    assert not await check(plugin, env)
    assert author(env)['timeline_backlog']['next_cursor'] == '5'
    assert author(env)['timeline_backlog']['cursor_failures'] == 1
    assert author(env)['since_id'] == '100' and not env.sent
    env.failure = ''
    assert await check(plugin, env)
    assert env.calls[-2:] == [5, 6]
    assert author(env)['since_id'] == '105'
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_fx_reload_can_fetch_details_evicted_from_memory_cache(harness, runtime):
    env = harness
    pages = fx_pages(env)
    detail_calls = []

    def respond(request):
        if '/2/status/' in request.url.path:
            tweet_id = request.url.path.rsplit('/', 1)[1]
            detail_calls.append(tweet_id)
            return httpx.Response(200, json={'code': 200, 'status': {
                'type': 'status', 'id': tweet_id, 'author': {'screen_name': 'tester'}, 'text': 'body',
            }})
        return pages(request)

    plugin = env.create(respond, provider='fxtwitter')
    await check(plugin, env)
    await plugin.twitter_api.close()
    plugin = env.create(respond, provider='fxtwitter')
    plugin.twitter_api.get_tweet = types.MethodType(runtime.api.TwitterAPI.get_tweet, plugin.twitter_api)
    assert await check(plugin, env)
    assert detail_calls == ['103', '104', '105']
    assert author(env)['since_id'] == '105'
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('providers', [('nitter', 'fxtwitter'), ('fxtwitter', 'nitter')])
async def test_provider_switch_preserves_items_but_restarts_cursor(harness, providers):
    env = harness
    readers = {'nitter': six_pages(env), 'fxtwitter': fx_pages(env)}
    plugin = env.create(readers[providers[0]], provider=providers[0])
    await check(plugin, env)
    before = author(env)['timeline_backlog']['items']
    await plugin.twitter_api.close()
    plugin = env.create(readers[providers[1]], provider=providers[1])
    await check(plugin, env)
    assert env.calls == [1, 2, 3, 4, 1, 2, 3, 4]
    assert author(env)['timeline_backlog']['items'] == before and not env.sent
    await check(plugin, env)
    assert author(env)['processed_tweet_ids'] == ['101', '102', '103', '104', '105']
    await plugin.twitter_api.close()
