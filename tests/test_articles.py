"""文章正常、降级与旧数据兼容路径，不调用真实模型或消息适配器。"""



import copy
import asyncio
import json
import sys
import types

import httpx
import pytest

from support.fakes import MediaImage as Image, Nodes, Plain, _delivery_settings, _message_settings


def status(blocks=True):
    return {
        "type": "status",
        "id": "100",
        "text": "附言 https://x.com/i/article/200",
        "author": {"screen_name": "author"},
        "article": {
            "id": "200",
            "title": "文章标题",
            "preview_text": "摘要",
            "cover_media": {
                "media_info": {
                    "original_img_url": "https://pbs.twimg.com/media/cover.jpg"
                }
            },
            "content": {
                "blocks": [
                    {
                        "type": "unstyled",
                        "text": "😀重点",
                        "inlineStyleRanges": [
                            {"offset": 2, "length": 2, "style": "Bold"}
                        ],
                    },
                    {"type": "unordered-list-item", "text": "列表内容"},
                ]
                if blocks
                else []
            },
        },
    }


def test_fx_article_text_media_and_utf16_styles(api_module):
    api = api_module.TwitterAPI(provider="fxtwitter")
    tweet = api._adapt_fxtwitter_status(status())
    assert "附言" in tweet["text"] and "• 列表内容" in tweet["text"]
    assert "【文章】文章标题" in tweet["text"]
    assert tweet["article"]["blocks"][0]["spans"] == [
        {"text": "😀"},
        {"text": "重点", "bold": True},
    ]
    assert tweet["images"] == ["https://pbs.twimg.com/media/cover.jpg"]
    quoted = api._adapt_fxtwitter_status(
        {"type": "status", "id": "300", "quote": status()}
    )
    assert quoted["quote"]["article"]["title"] == "文章标题"
    assert "列表内容" in quoted["quote"]["text"]


def test_article_lead_preserves_links_to_other_articles(api_module):
    raw = status()
    raw["text"] = (
        "参考 https://x.com/i/article/201 https://x.com/i/article/2000 "
        "https://x.com/i/article/200?from=post"
    )
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    assert tweet["article_lead"] == (
        "参考 https://x.com/i/article/201 https://x.com/i/article/2000"
    )
    assert "https://x.com/i/article/201" in tweet["text"]


def test_fx_article_atomic_media_reuses_video_selection(api_module):
    raw = status()
    article = raw["article"]
    article["media_entities"] = [
        {
            "media_id": "image",
            "media_info": {
                "__typename": "ApiImage",
                "original_img_url": "https://pbs.twimg.com/media/inline.jpg",
            },
        },
        {
            "media_id": "video",
            "media_info": {
                "__typename": "ApiVideo",
                "media_url_https": "https://pbs.twimg.com/media/poster.jpg",
                "variants": [
                    {"url": "https://video.twimg.com/low.mp4", "bit_rate": 100},
                    {"url": "https://video.twimg.com/high.mp4", "bit_rate": 200},
                ],
            },
        },
    ]
    article["content"]["entityMap"] = [
        {
            "key": "0",
            "value": {
                "type": "MEDIA",
                "data": {"mediaItems": [{"mediaId": "image"}, {"mediaId": "video"}]},
            },
        },
        {
            "key": "1",
            "value": {"type": "MARKDOWN", "data": {"markdown": "print('sample')"}},
        },
    ]
    article["content"]["blocks"] += [
        {"type": "atomic", "entityRanges": [{"key": 0}]},
        {"type": "atomic", "entityRanges": [{"key": 1}]},
    ]
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    assert tweet["videos"] == ["https://video.twimg.com/high.mp4"]
    assert "https://pbs.twimg.com/media/inline.jpg" in tweet["images"]
    assert tweet["article"]["blocks"][-1]["type"] == "code-block"
    assert (
        tweet["video_previews"][0]["poster"] == "https://pbs.twimg.com/media/poster.jpg"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_ok", [True, False])
async def test_fx_summary_cache_fetches_detail_without_caching_retweeter(
    api_module, detail_ok
):
    api = api_module.TwitterAPI(provider="fxtwitter")
    cached = status(False)
    cached["reposted_by"] = {"screen_name": "retweeter"}
    api._cache_fxtwitter_status(cached)
    calls = []

    async def request(path):
        calls.append(path)
        return {"status": status()} if detail_ok else None

    api._request_fxtwitter_json = request
    result = await api.get_tweet("author", "100")
    assert calls == ["2/status/100"]
    assert result["status"] and result["retweet"] is None
    assert bool(result["article"]["blocks"]) == detail_ok
    assert ("未获取到全文" in result["text"]) != detail_ok


CARD = '<div class="article-card"><a class="card-container" href="/i/article/100"><h2 class="card-title">标题</h2><p class="card-description">摘要</p></a></div>'
ARTICLE = '<article class="article-body"><h1 class="article-title">标题</h1><div class="article-author"><p>作者信息不应成为正文</p></div><p>第一段<br>第二行<strong>重点</strong></p><ul><li>列表</li></ul></article>'


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "unsupported", "error", "legacy", "quote"])
async def test_nitter_article_detail_and_legacy_fallback(api_module, mode):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test")
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if "/i/article/" in request.url.path:
            if mode == "error":
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(
                200,
                text=ARTICLE if mode in ("full", "quote") else "Unsupported feature",
            )
        card = "" if mode == "legacy" else CARD
        quote = f'<div class="quote"><a class="quote-link" href="/author/status/100"></a><div class="quote-text">引用附言</div>{card}</div>'
        body = f'<div class="main-tweet"><a class="username">@author</a><div class="tweet-content media-body">正文</div>{quote if mode == "quote" else card}</div>'
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._get_client = lambda: async_value(client)
        result = await api.get_tweet("author", "300" if mode == "quote" else "100")
    assert result["status"]
    target = result["quote"] if mode == "quote" else result
    if mode == "legacy":
        assert "article" not in target and len(requests) == 1
    else:
        assert requests[-1] == "https://nitter.test/i/article/100"
        assert "作者信息" not in target["text"]
        assert bool(target["article"]["blocks"]) == (mode in ("full", "quote"))
        if mode == "quote":
            assert "article" not in result
        if mode == "full":
            assert "第一段\n第二行重点" in target["text"]


async def async_value(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["orig", "large"])
@pytest.mark.parametrize("cover_parent", ["none", "article", "image"])
async def test_nitter_article_images_use_image_sources(api_module, quality, cover_parent):
    api = api_module.TwitterAPI(nitter_url="https://nitter.test", image_quality=quality)
    cover = '<img class="article-cover" src="/pic/cover.jpg">'
    if cover_parent != "none":
        href = "/i/article/100" if cover_parent == "article" else "/pic/orig/cover.jpg"
        cover = f'<a href="{href}">{cover}</a>'
    card = CARD.replace('<h2', '<img src="/pic/card.jpg"><h2')
    body = f'<div class="main-tweet"><div class="tweet-content media-body"></div>{card}</div>'
    full = cover + '<article class="article-body"><h1 class="article-title">标题</h1><img src="/pic/body.jpg"><p>正文</p></article>'

    def respond(request):
        return httpx.Response(200, text=full if "/i/article/" in request.url.path else body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api._get_client = lambda: async_value(client)
        tweet = await api.get_tweet("author", "100")
    expected = "/pic/orig/cover.jpg" if cover_parent == "image" and quality == "orig" else "/pic/cover.jpg"
    assert tweet["article"]["cover"] == "https://nitter.test" + expected
    assert tweet["images"] == ["https://nitter.test" + expected, "https://nitter.test/pic/body.jpg"]
    assert all("/i/article/" not in url for url in tweet["images"])


def test_article_named_links_are_preserved_for_both_sources(api_module):
    raw = status()
    raw["article"]["content"] = {
        "blocks": [{"type": "unstyled", "text": "😀下载报告", "entityRanges": [{"key": 0, "offset": 2, "length": 4}]}],
        "entityMap": [{"key": "0", "value": {"type": "LINK", "data": {"url": "https://example.com/report.pdf"}}}],
    }
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    assert "下载报告\nhttps://example.com/report.pdf" in tweet["text"]
    block = tweet["article"]["blocks"][0]
    assert block["spans"] == [{"text": "😀"}, {"text": "下载报告", "href": "https://example.com/report.pdf"}]
    assert block["links"] == ["https://example.com/report.pdf"]
    module = sys.modules[api_module.__package__ + ".twitter_article"]
    article = module.parse_nitter_article(
        '<article class="article-body"><p><a href="/report.pdf">下载<strong>报告</strong></a><a href="javascript:bad()">坏链接</a></p></article>',
        {"title": "标题"}, lambda _img: "", lambda url: "https://nitter.test" + url if url.startswith("/") else url,
    )
    assert article["blocks"][0]["links"] == ["https://nitter.test/report.pdf"]
    assert "下载报告坏链接\nhttps://nitter.test/report.pdf" in module.article_text(article)
    assert "javascript:" not in module.article_text(article)


@pytest.mark.parametrize("entry", ["tweet-link", "unavailable-box", "date-only"])
def test_nitter_embedded_tweet_keeps_position_and_matches_fx_fallback(api_module, entry):
    module = sys.modules[api_module.__package__ + ".twitter_article"]
    primary = '' if entry == 'date-only' else f'<a class="{entry}" href="/embedded/status/123#m"></a>'
    # 对齐 8142bab 的 renderTweet 结构，保留重复入口及嵌套引用以验证不会误取。
    embedded = f'''<div class="timeline-item" data-username="embedded">{primary}
      <div class="tweet-body"><div class="tweet-header"><span class="tweet-date">
        <a href="/embedded/status/123#m">date</a></span></div>
        <div class="tweet-content media-body">不应展开的推文正文</div>
        <p>不应纳入的媒体说明</p><img src="/pic/embedded.jpg">
        <div class="quote"><span class="tweet-date"><a href="/quoted/status/456">quote date</a></span><p>引用正文</p></div>
      </div></div>'''
    body = f'<article class="article-body"><h1 class="article-title">标题</h1><p>前文</p>{embedded}<p>后文</p></article>'
    article = module.parse_nitter_article(body, {"title": "标题"}, lambda img: img.get("src"), lambda url: url)
    assert [b["text"] for b in article["blocks"]] == ["前文", "https://x.com/i/status/123", "后文"]
    assert "456" not in module.article_text(article)
    assert "不应" not in module.article_text(article)
    raw = status()
    raw["article"]["content"] = {
        "blocks": [{"type": "atomic", "entityRanges": [{"key": 0}]}],
        "entityMap": [{"key": "0", "value": {"type": "TWEET", "data": {"tweetId": "123"}}}],
    }
    fx = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    assert article["blocks"][1] == fx["article"]["blocks"][0]


def test_nitter_multiple_embedded_tweets_keep_each_container_once(api_module):
    module = sys.modules[api_module.__package__ + ".twitter_article"]
    body = '<article class="article-body"><p>前文</p>'
    for tweet_id in ("123", "789", "123"):
        body += f'<div class="timeline-item"><a class="tweet-link" href="/author/status/{tweet_id}"></a><div class="timeline-item"><a class="tweet-link" href="/nested/status/999"></a><p>嵌套正文</p></div><p>正文</p></div>'
    body += '<p>后文</p></article>'
    article = module.parse_nitter_article(body, {"title": "标题"}, lambda _img: "", lambda url: url)
    assert [b["text"] for b in article["blocks"]] == [
        "前文", "https://x.com/i/status/123", "https://x.com/i/status/789", "https://x.com/i/status/123", "后文",
    ]


def test_ordered_list_numbers_survive_article_pages(media_plugin_module):
    renderer = sys.modules[f"{media_plugin_module.__package__}.twitter_renderer"]
    blocks = [{"type": "ordered-list-item", "text": str(i) + "正文" * 180, "spans": [{"text": str(i) + "正文" * 180}]} for i in range(1, 9)]
    article = {"title": "列表", "blocks": blocks, "cover": ""}
    context = renderer.build_tweet_card_context("author", {"article": article})
    pages = renderer.tweet_card_page_contexts(context)
    assert len(pages) > 1
    assert [b["number"] for p in pages for b in p["tweet"]["article"]["blocks"]] == list(range(1, 9))
    assert "number" not in blocks[0]


def make_service(media_plugin_module, **settings):
    return media_plugin_module.TweetMessageService(
        object(),
        object(),
        None,
        _message_settings(media_plugin_module, pre_download_media=False, **settings),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_article_translation_keeps_block_structure(
    media_plugin_module, api_module, valid
):
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(
        status()
    )
    service = make_service(media_plugin_module, translate_enabled=True)
    calls = []
    service.get_translate_provider_id = lambda _umo: async_value("model")

    async def translate(text, provider_id, structured=False):
        assert structured
        calls.append(json.loads(text))
        return (
            json.dumps(["译文" + s for s in calls[-1]], ensure_ascii=False)
            if valid
            else "结构错误",
            "model",
        )

    service._translate_with_provider = translate
    translated, model = await service.maybe_translate(tweet, "session")
    assert len(calls) == 1 and all("https://x.com/i/article" not in t for t in calls[0])
    if valid:
        assert model == "model" and "• 译文列表内容" in translated
        assert "https://x.com/i/article/200" not in translated
        assert tweet["translated_article"]["blocks"][1]["type"] == "unordered-list-item"
    else:
        assert (
            translated is None and model is None and "translated_article" not in tweet
        )


@pytest.mark.asyncio
async def test_link_only_translation_is_not_called(media_plugin_module):
    service = make_service(media_plugin_module, translate_enabled=True)
    assert await service.maybe_translate(
        {"text": "https://x.com/i/article/200"}, "session"
    ) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [True, False])
async def test_article_screenshot_failure_returns_full_text(
    media_plugin_module, api_module, media
):
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(
        status()
    )
    service = make_service(media_plugin_module, send_media_separately=media)

    async def render(*args, **kwargs):
        raise RuntimeError("render unavailable")

    service.html_render = render
    chain = await service.build_message_chain("author", tweet)
    assert "【文章】文章标题" in chain[0].text and "列表内容" in chain[0].text
    assert len(chain) == (2 if media else 1)


def test_article_context_no_duplicate_cover_and_lossless_paging(
    media_plugin_module, api_module
):
    renderer = sys.modules[f"{media_plugin_module.__package__}.twitter_renderer"]
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(
        status()
    )
    context = renderer.build_tweet_card_context("author", tweet)
    assert context["tweet"]["media"] == []
    assert renderer.tweet_card_render_options(context)["full_page"]
    original = copy.deepcopy(context)
    assert renderer.tweet_card_page_contexts(context) == [context]
    block = {"type": "paragraph", "text": "长文" * 4000, "spans": []}
    context["tweet"]["article"]["blocks"] = [block]
    pages = renderer.tweet_card_page_contexts(context)
    assert len(pages) > 1
    assert (
        "".join(b["text"] for p in pages for b in p["tweet"]["article"]["blocks"])
        == block["text"]
    )
    assert all(not p["tweet"]["article"]["cover"] for p in pages[1:])
    assert original["tweet"]["article"]["cover"]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_link", [False, True])
@pytest.mark.parametrize("mode", ["text", "screenshot", "fallback"])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("translated", [False, True])
async def test_article_footer_obeys_link_setting_without_removing_body_links(
    media_plugin_module, api_module, include_link, mode, quoted, translated,
):
    jinja = pytest.importorskip("jinja2")
    raw = status()
    raw["text"] = "附言 https://x.com/i/article/201 https://x.com/i/article/200"
    raw["article"]["content"]["blocks"].append({"type": "unstyled", "text": "正文 https://example.com/body"})
    target = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    if translated:
        target["translated_article"] = copy.deepcopy(target["article"])
        target["translated_article"]["title"] = "译文标题"
        target["translated_article_lead"] = "译文 " + target["article_lead"]
        target["translated_text"] = "译文"
    tweet = {"username": "poster", "tweet_id": "300", "text": "引用附言", "quote": target} if quoted else target
    service = make_service(
        media_plugin_module, include_tweet_link=include_link, send_media_separately=False,
        text_render_mode="text" if mode == "text" else "screenshot",
    )
    rendered = []

    async def render(template, context, options):
        if mode == "fallback":
            raise RuntimeError("render unavailable")
        rendered.append(jinja.Template(template).render(**context))
        return "https://example.com/screenshot.png"

    service.html_render = render
    chain = await service.build_message_chain(
        "author", tweet, translated_text=target.get("translated_text") if not quoted else None,
    )
    content = "\n".join(rendered) if mode == "screenshot" else chain[0].text
    assert ("https://x.com/i/article/200" in content) is include_link
    assert "https://x.com/i/article/201" in content
    assert "https://example.com/body" in content
    assert "译文标题" in content if translated else "文章标题" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["plain", "node", "collective"])
async def test_failed_article_page_keeps_cursor_and_only_retries_failed_session(media_plugin_module, api_module, transport):
    raw = status()
    raw["article"]["content"]["blocks"] = [
        {"type": "ordered-list-item", "text": "段落" * 180} for _ in range(8)
    ]
    raw["reposted_by"] = {"screen_name": "tester"}
    tweet = api_module.TwitterAPI(provider="fxtwitter")._adapt_fxtwitter_status(raw)
    store = {"twitter_subs": {"tester": {"since_id": "99", "subscribers": {
        umo: {"status": True, "r18": True} for umo in ("good", "bad")
    }}}}
    recovered, calls = False, []

    async def get_kv(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_kv(key, value):
        store[key] = copy.deepcopy(value)

    async def send(umo, message):
        chain = message.chain
        calls.append((umo, chain))
        if any(isinstance(part, Nodes) for part in chain):
            return False  # 同时覆盖单条和集体转发的普通消息降级。
        if len(chain) > 1 and any(isinstance(part, Image) for part in chain):
            return False
        if len(chain) == 1 and isinstance(chain[0], Image):
            return recovered or umo == "good" or "page-1.png" in chain[0].file
        return True  # 链接成功不能替代失败的正文页。

    async def timeline(*args):
        return [{"tweet_id": "100", "username": "author", "is_retweet": True, "retweeter_username": "tester"}]

    async def detail(*args):
        return copy.deepcopy(tweet)

    async def render(template, context, options):
        title = context["tweet"]["article"]["title"]
        page = "1" if "续 " not in title else title.split("续 ")[1].split("/")[0]
        return f"https://example.com/page-{page}.png"

    api = types.SimpleNamespace(get_user_timeline_items=timeline, get_tweet=detail)
    subscriptions = media_plugin_module.SubscriptionService(get_kv, put_kv, api, lambda: True)
    messages = make_service(media_plugin_module, include_tweet_link=True, send_media_separately=False)
    messages.html_render = render
    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send), subscriptions, messages,
        _delivery_settings(media_plugin_module, use_node=transport != "plain", collective_forward=transport == "collective", deduplicate_retweets=True),
    )
    polling = media_plugin_module.PollingService(api, subscriptions, delivery, media_plugin_module.PollingSettings(
        include_retweets=True, data_provider="fxtwitter", custom_nitter_url="", website_list=(),
    ))
    await polling.check_user("tester", copy.deepcopy(store["twitter_subs"]["tester"]))
    await polling.flush_pending_collective()
    author = store["twitter_subs"]["tester"]
    assert author["since_id"] == "99"
    assert author["subscribers"]["good"]["pending_delivery_ids"] == ["100"]
    assert not author["subscribers"]["bad"].get("pending_delivery_ids")
    assert not author["subscribers"]["bad"].get("recent_deliveries")
    assert store.get("twitter_retweet_dedup_seen", {}).get("bad", []) == []
    assert author["pending_tweet_items"][0]["tweet_id"] == "100"
    recovered = True
    calls.clear()
    await polling.check_user("tester", copy.deepcopy(author))
    await polling.flush_pending_collective()
    assert store["twitter_subs"]["tester"]["since_id"] == "100"
    assert not any(umo == "good" for umo, _chain in calls)
    assert store["twitter_retweet_dedup_seen"]["bad"] == ["100"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["second-page", "extra"])
async def test_screenshot_receipt_only_after_all_pages_before_extra_media(media_plugin_module, cancel_at):
    module = sys.modules[f"{media_plugin_module.__package__}.services.tweet_message_service"]
    pages = [Image.fromURL(f"https://example.com/{i}.png") for i in (1, 2)]
    extra = Image.fromURL("https://example.com/extra.png")
    chain = module.TweetMessageChain([*pages, Plain("https://x.com/author/status/100"), extra], required_images=pages)
    receipts = []

    async def send(_umo, message):
        if len(message.chain) > 1:
            return False
        part = message.chain[0]
        if part is (pages[1] if cancel_at == "second-page" else extra):
            raise asyncio.CancelledError
        return True

    delivery = media_plugin_module.TweetDeliveryService(
        types.SimpleNamespace(send_message=send), object(), object(), _delivery_settings(media_plugin_module),
    )
    plain, _videos = delivery.split_plain_chain_and_videos(chain)
    with pytest.raises(asyncio.CancelledError):
        await delivery.send_plain_chain_resilient("session", plain, on_primary_sent=lambda: receipts.append("sent"))
    assert receipts == (["sent"] if cancel_at == "extra" else [])
