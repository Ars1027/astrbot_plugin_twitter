import httpx
import pytest

from test_fxtwitter_api import api_module as api_module


def html_page(ids, cursor=None):
    items = "".join(
        f'<div class="timeline-item"><a class="tweet-link" href="/tester/status/{i}"></a></div>'
        for i in ids
    )
    more = (
        f'<div class="show-more"><a href="?cursor={cursor}">Load more</a></div>'
        if cursor
        else ""
    )
    return f'<div class="timeline">{items}{more}</div>'


@pytest.mark.asyncio
async def test_nitter_reads_all_pages_before_returning_increment(api_module):
    calls = []

    def respond(request):
        calls.append(request.url.params.get("cursor", ""))
        return httpx.Response(
            200,
            text=html_page([102, 101, 100])
            if calls[-1]
            else html_page([104, 103], "page2"),
        )

    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        items = await api.get_user_timeline_items("tester", "100")
    assert [item["tweet_id"] for item in items] == ["101", "102", "103", "104"]
    assert calls == ["", "page2"]


@pytest.mark.asyncio
async def test_nitter_later_page_failure_never_returns_partial_increment(api_module):
    def respond(request):
        return (
            httpx.Response(503)
            if request.url.params
            else httpx.Response(200, text=html_page([104], "page2"))
        )

    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        with pytest.raises(RuntimeError):
            await api.get_user_timeline_items("tester", "100")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        "<html>captcha</html>",
        '<div class="timeline"></div>',
        '<div class="timeline"><div class="timeline-item">invalid tweet</div></div>',
        html_page([101], "ok").replace(
            "?cursor=ok", "https://evil.test/tester?cursor=ok"
        ),
        html_page([101], "ok").replace("?cursor=ok", "/other?cursor=ok"),
        html_page([101], "ok").replace("?cursor=ok", "?wrong=ok"),
    ],
)
async def test_rejects_invalid_pages_and_next_links(api_module, html):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))
    ) as client:
        api._client = client
        with pytest.raises(api_module.TwitterTimelineError):
            await api.get_user_timeline_page("tester")


@pytest.mark.asyncio
async def test_pins_old_retweets_and_load_newest_do_not_end_scan(api_module):
    first = """<div class="timeline">
        <div class="timeline-item"><i class="icon-pin"></i><a class="tweet-link" href="/tester/status/90"></a></div>
        <div class="timeline-item"><div class="retweet-header">Tester retweeted</div><a class="tweet-link" href="/original/status/80"></a></div>
        <div class="timeline-item"><a class="tweet-link" href="/tester/status/104"></a></div>
        <div class="timeline-item show-more"><a href="/tester">Load newest</a></div>
        <div class="show-more"><a href="?cursor=opaque%2B%2F%3D">Load more</a></div>
    </div>"""
    calls = []

    def respond(request):
        calls.append(request.url.params.get("cursor", ""))
        # 整页处理：旧普通推文之后仍有新的条目，不得提前 break。
        return httpx.Response(
            200, text=first if len(calls) == 1 else html_page([100, 103, 102, 104])
        )

    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        assert [
            i["tweet_id"] for i in await api.get_user_timeline_items("tester", "100")
        ] == ["102", "103", "104"]
    assert calls == ["", "opaque+/="]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["timeline-none", "timeline-end"])
async def test_explicit_empty_timeline_is_exhausted(api_module, marker):
    html = f'<div class="timeline"><h2 class="{marker}">No items</h2></div>'
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))
    ) as client:
        api._client = client
        page = await api.get_user_timeline_page("tester")
    assert page.items == [] and page.exhausted


@pytest.mark.asyncio
async def test_stateless_budget_does_not_return_partial_and_initial_follow_only_reads_head(
    api_module,
):
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(200, text=html_page([200 - len(calls)], str(len(calls))))

    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._client = client
        with pytest.raises(api_module.TwitterTimelineError, match="预算"):
            await api.get_user_timeline_items("tester", "100")
        assert len(calls) == 4
        assert await api.get_user_newtimeline("tester") == ["195"]
        assert len(calls) == 5
