import asyncio
from dataclasses import replace

import httpx
import pytest

from test_nitter_timeline import html_page
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
