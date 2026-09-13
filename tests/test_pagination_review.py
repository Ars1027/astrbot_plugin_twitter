import asyncio
import copy
from dataclasses import replace

import httpx
import pytest

from test_nitter_timeline import html_page
from test_fxtwitter_api import _fixture
from test_timeline_backlog import author, check, harness as harness, runtime as runtime


def timeline(ids, retweets=(), cursor=None):
    html = html_page(ids, cursor)
    for tweet_id in retweets:
        link = f'<a class="tweet-link" href="/tester/status/{tweet_id}">'
        html = html.replace(
            link, '<div class="retweet-header">Tester retweeted</div>' + link
        )
    return html


def fx_response(ids, cursor=None, retweets=()):
    return httpx.Response(
        200,
        json={
            "code": 200,
            "results": [
                dict(
                    type="status",
                    id=str(i),
                    text=f"body {i}",
                    author={"screen_name": "tester"},
                    **(
                        {"reposted_by": {"screen_name": "tester"}}
                        if i in retweets
                        else {}
                    ),
                )
                for i in ids
            ],
            "cursor": {"bottom": cursor},
        },
    )


@pytest.mark.asyncio
async def test_unrelated_commit_keeps_unacknowledged_backlog(harness):
    env = harness
    plugin = env.create(lambda _: httpx.Response(200, text=timeline([110, 105, 100])))
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    await plugin.subscription_service.commit_processed_tweets("tester", ["200"], "200")
    assert {i["tweet_id"] for i in author(env)["timeline_backlog"]["items"]} == {
        "105",
        "110",
    }
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_later_retweet_above_head_id_is_kept(harness):
    env = harness

    def respond(request):
        return httpx.Response(
            200,
            text=timeline([110], [110], "2")
            if not request.url.params
            else timeline([120, 100], [120]),
        )

    plugin = env.create(respond)
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["120", "110"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
async def test_timeline_order_and_smaller_pending_id_survive_reload(
    harness, collective
):
    env = harness

    def respond(_request):
        return httpx.Response(200, text=timeline([110, 120, 100], [110]))

    plugin = env.create(respond, collective)
    plugin.polling_service.settings = replace(
        plugin.polling_service.settings, max_tweets_per_user=1
    )
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert author(env)["processed_tweet_ids"] == ["120"]
    assert [i["tweet_id"] for i in author(env)["timeline_backlog"]["items"]] == ["110"]
    await plugin.twitter_api.close()
    plugin = env.create(
        lambda _: pytest.fail("ready backlog must not refetch"), collective
    )
    await check(plugin, env)
    await plugin.polling_service.flush_pending_collective()
    assert author(env)["processed_tweet_ids"] == ["120", "110"]
    assert author(env)["since_id"] == "120"
    assert "timeline_backlog" not in author(env)
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_nitter_backlog_is_preserved_when_switching_to_fx(harness, ready):
    env = harness
    plugin = env.create(
        lambda _: httpx.Response(
            200, text=timeline([110, 105], cursor=None if ready else "2")
        )
    )
    plugin.polling_service.timeline_backlog.MAX_PAGES = 1
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    await plugin.twitter_api.close()
    calls = []

    def respond(request):
        calls.append(request.url)
        assert not ready, "ready backlog should not refetch after source change"
        return fx_response([115, 200, 100], retweets=[115])

    plugin = env.create(respond, provider="fxtwitter")
    await check(plugin, env)
    assert author(env)["processed_tweet_ids"] == (
        ["105", "110"] if ready else ["105", "110", "200", "115"]
    )
    assert len(calls) == (0 if ready else 1)
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_source_switch_failure_keeps_partial_backlog(harness):
    env = harness
    plugin = env.create(lambda _: httpx.Response(200, text=timeline([110], cursor="2")))
    plugin.polling_service.timeline_backlog.MAX_PAGES = 1
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    await plugin.twitter_api.close()
    plugin = env.create(lambda _: httpx.Response(429), provider="fxtwitter")
    assert not await check(plugin, env)
    assert author(env)["since_id"] == "100"
    assert [i["tweet_id"] for i in author(env)["timeline_backlog"]["items"]] == ["110"]
    assert not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_rescan_repositions_items_keeps_missing_and_accepts_new(harness):
    env = harness
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(
            200,
            text=timeline([130, 110], cursor="2")
            if request.url.host == "nitter.test"
            else timeline([140, 110, 120, 100], retweets=[110]),
        )

    plugin = env.create(respond)
    plugin.polling_service.timeline_backlog.MAX_PAGES = 1
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    plugin.twitter_api.nitter_url = "https://other.test"
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["130", "120", "110", "140"]
    assert not batch.items[2]["is_retweet"]  # 元数据仍取首次，位置使用本次扫描。
    assert calls == ["nitter.test", "other.test"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["scanning", "ready", "blocked"])
async def test_v1_migration_rescans_without_readding_confirmed_ids(harness, phase):
    env = harness
    calls = []

    def respond(_request):
        calls.append(1)
        return httpx.Response(200, text=timeline([110, 120, 105, 100], retweets=[110]))

    plugin = env.create(respond)
    item = dict(
        tweet_id="110",
        username="tester",
        is_retweet=True,
        retweeter_username="tester",
        retweeter_screen_name="original",
    )
    data = env.read()["twitter_subs"]
    data["tester"].update(
        since_id="105",
        processed_tweet_ids=["105"],
        timeline_backlog=dict(
            version=1,
            generation="old",
            source="nitter:https://nitter.test",
            anchor_since_id="100",
            upper_id=110,
            phase=phase,
            next_cursor="old-cursor",
            seen_cursors=[""],
            items=[item],
            cursor_failures=0,
            blocked_reason="capacity",
            required=dict(items=6000, bytes=1, cursors=1),
        ),
    )
    await env.put_kv("twitter_subs", data)
    service = plugin.polling_service.timeline_backlog
    if phase == "ready":
        before = env.read()
        env.writes_fail = True
        with pytest.raises(OSError):
            await service.get_batch("tester")
        assert env.read() == before and not calls
        env.writes_fail = False
    batch = await service.get_batch("tester")
    if phase == "blocked":
        assert batch.pending and not calls
        assert author(env)["timeline_backlog"]["items"] == [item]
        service.MAX_ITEMS = 6000
        batch = await service.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["120", "110"]
    state = author(env)["timeline_backlog"]
    assert state["version"] == 2 and state["anchor_since_id"] == "100"
    assert "upper_id" not in state and calls == [1]
    assert batch.items[1]["retweeter_screen_name"] == "original"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_concurrent_ack_of_inflight_id_rejects_page_and_prevents_replay(harness):
    env = harness
    entered, resume = asyncio.Event(), asyncio.Event()

    async def respond(_request):
        entered.set()
        await resume.wait()
        return httpx.Response(200, text=timeline([120, 110, 100]))

    plugin = env.create(respond)
    service = plugin.polling_service.timeline_backlog
    task = asyncio.create_task(service.get_batch("tester"))
    await entered.wait()
    await plugin.subscription_service.commit_processed_tweets("tester", ["110"], "110")
    resume.set()
    assert (await task).pending
    batch = await service.get_batch("tester")
    assert [i["tweet_id"] for i in batch.items] == ["120"]
    assert not env.sent
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_fx_cache_matches_first_item_on_same_and_later_pages(harness, background):
    env = harness
    first = _fixture("fxtwitter_timeline_page1.json")
    second = _fixture("fxtwitter_timeline_page2.json")
    duplicate = copy.deepcopy(first["results"][0])
    duplicate["text"] = "later page duplicate"
    second["results"].insert(0, duplicate)

    def respond(request):
        return httpx.Response(
            200, json=second if request.url.params.get("cursor") else first
        )

    plugin = env.create(respond, provider="fxtwitter")
    del plugin.twitter_api.get_tweet  # 使用真实详情缓存与适配。
    if background:
        await plugin.polling_service.timeline_backlog.get_batch("tester")
    else:
        await plugin.twitter_api.get_user_timeline_items("tester", "101")
    assert (await plugin.twitter_api.get_tweet("tester", "105"))["text"] == "five"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_fx_exact_retweet_anchor_finishes_on_first_page(harness):
    env = harness
    calls = []

    def respond(request):
        page = int(request.url.params.get("cursor", "1"))
        calls.append(page)
        return fx_response(
            [110, 100] if page == 1 else [99 - page],
            str(page + 1),
            retweets=[110, 100, 99 - page],
        )

    plugin = env.create(respond, provider="fxtwitter")
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert not batch.pending and calls == [1]
    assert [i["tweet_id"] for i in batch.items] == ["110"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_new_scan_can_refresh_previously_cached_details(harness, background):
    env = harness
    payload = _fixture("fxtwitter_timeline_page1.json")
    payload["cursor"]["bottom"] = None
    plugin = env.create(
        lambda _: httpx.Response(200, json=payload), provider="fxtwitter"
    )
    del plugin.twitter_api.get_tweet
    if background:
        await plugin.polling_service.timeline_backlog.get_batch("tester")
    else:
        await plugin.twitter_api.get_user_timeline_items("tester", "100")
    assert (await plugin.twitter_api.get_tweet("tester", "105"))["text"] == "five"
    payload["results"][0]["text"] = "refreshed"
    if background:
        # 另一个订阅的独立扫描可以刷新同一原帖，不能把全局缓存永久冻结。
        data = env.read()["twitter_subs"]
        data["other"] = dict(since_id="100", subscribers={"group": {"status": True}})
        await env.put_kv("twitter_subs", data)
        await plugin.polling_service.timeline_backlog.get_batch("other")
    else:
        await plugin.twitter_api.get_user_timeline_items("tester", "100")
    assert (await plugin.twitter_api.get_tweet("tester", "105"))["text"] == "refreshed"
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_failed_page_save_does_not_overwrite_cache_or_persist_details(harness):
    env = harness
    payload = _fixture("fxtwitter_timeline_page1.json")
    payload["cursor"]["bottom"] = None
    fail = True

    def respond(_request):
        env.writes_fail = fail
        return httpx.Response(200, json=payload)

    plugin = env.create(respond, provider="fxtwitter")
    plugin.twitter_api._cache_fxtwitter_status(
        dict(payload["results"][0], text="existing")
    )
    with pytest.raises(OSError):
        await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert plugin.twitter_api._status_cache["105"]["text"] == "existing"
    assert not author(env)["timeline_backlog"]["items"]
    fail = False
    env.writes_fail = False
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert plugin.twitter_api._status_cache["105"]["text"] == "five"
    for item in author(env)["timeline_backlog"]["items"]:
        assert "text" not in item and "media" not in item and "statuses" not in item
    await plugin.twitter_api.close()


@pytest.mark.asyncio
async def test_reload_does_not_cache_later_duplicate_as_first_details(harness):
    env = harness
    first = _fixture("fxtwitter_timeline_page1.json")
    second = _fixture("fxtwitter_timeline_page2.json")
    second["results"].insert(0, dict(first["results"][0], text="late duplicate"))
    details = []

    def respond(request):
        if request.url.path.endswith("/2/status/105"):
            details.append("105")
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "status": dict(first["results"][0], text="fresh detail"),
                },
            )
        return httpx.Response(
            200, json=second if request.url.params.get("cursor") else first
        )

    plugin = env.create(respond, provider="fxtwitter")
    plugin.polling_service.timeline_backlog.MAX_PAGES = 1
    assert (await plugin.polling_service.timeline_backlog.get_batch("tester")).pending
    await plugin.twitter_api.close()
    plugin = env.create(respond, provider="fxtwitter")
    del plugin.twitter_api.get_tweet
    await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert "105" not in plugin.twitter_api._status_cache
    assert (await plugin.twitter_api.get_tweet("tester", "105"))[
        "text"
    ] == "fresh detail"
    assert details == ["105"]
    await plugin.twitter_api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,old_id", [("fxtwitter", 95), ("nitter", 100)])
async def test_smaller_retweet_and_nitter_exact_id_do_not_end_scan(
    harness, provider, old_id
):
    env = harness
    calls = []

    def respond(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        ids = [90] if cursor else [110, old_id]
        if provider == "fxtwitter":
            return fx_response(
                ids, None if cursor else "2", retweets=[] if cursor else ids
            )
        return httpx.Response(
            200,
            text=timeline(
                ids, retweets=[] if cursor else ids, cursor=None if cursor else "2"
            ),
        )

    plugin = env.create(respond, provider=provider)
    batch = await plugin.polling_service.timeline_backlog.get_batch("tester")
    assert not batch.pending and calls == [None, "2"]
    assert [i["tweet_id"] for i in batch.items] == ["110"]
    await plugin.twitter_api.close()
