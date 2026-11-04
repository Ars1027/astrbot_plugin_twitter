"""截图模式的推文卡片渲染辅助方法。"""

from __future__ import annotations

import math
import copy
import unicodedata
from pathlib import Path


TWEET_CARD_TEMPLATE_PATH = Path(__file__).with_name("asset") / "tweet_card.html"
TWEET_CARD_WIDTH = 760
TWEET_CARD_RENDER_SCALE = 1.5


def tweet_card_page_contexts(context: dict) -> list[dict]:
    """文章按块分页，避免超长单图；短推文继续使用同一渲染契约。"""
    tweet = context["tweet"]
    all_pages = []
    for target in [tweet, tweet.get("quote") or {}]:
        article = target.get("article")
        pages, current, weight = [], [], 0
        number = 0
        for original_block in (article or {}).get("blocks") or []:
            block = dict(original_block)
            if block.get("type") == "ordered-list-item":
                number += 1
                block["number"] = number
            else:
                number = 0
            text = block.get("text") or ""
            parts = [block]
            if len(text) > 1200:
                parts = [{**block, "text": text[i:i + 1200], "spans": _slice_spans(block, i, i + 1200)}
                         for i in range(0, len(text), 1200)]
            for part in parts:
                cost = _display_units(part.get("text") or "") + 120
                if part.get("type") in ("image", "video"):
                    cost += 1500
                if current and weight + cost > 3600:
                    pages.append(current)
                    current, weight = [], 0
                current.append(part)
                weight += cost
        if current:
            pages.append(current)
        all_pages.append(pages or [[]])
    count = max(map(len, all_pages))
    contexts = []
    for index in range(count):
        page = copy.deepcopy(context)
        for target, pages in zip([page["tweet"], page["tweet"].get("quote") or {}], all_pages):
            article = target.get("article")
            if index:
                target["text"], target["media"] = "", []
            if not article:
                continue
            if index >= len(pages):
                target["article"] = None
                continue
            article["blocks"] = pages[index]
            if index:
                article["cover"] = ""
                article["title"] += f"（续 {index + 1}/{len(pages)}）"
        quote = page["tweet"].get("quote")
        if index and quote and not (quote.get("text") or quote.get("media") or quote.get("article")):
            page["tweet"]["quote"] = None
        contexts.append(page)
    return contexts


def _slice_spans(block: dict, start: int, end: int) -> list[dict]:
    result, offset = [], 0
    for span in block.get("spans") or [{"text": block.get("text") or ""}]:
        text = span["text"]
        piece = text[max(0, start - offset):max(0, end - offset)]
        if piece:
            result.append({**span, "text": piece})
        offset += len(text)
        if offset >= end:
            break
    return result


def load_tweet_card_template() -> str:
    """读取 AstrBot html_render 使用的 Jinja2 HTML 模板。"""
    return TWEET_CARD_TEMPLATE_PATH.read_text(encoding="utf-8")


def tweet_card_render_options(context: dict | None = None) -> dict:
    """生成传给 AstrBot html_render 的 Playwright 截图选项。"""
    options = {
        "type": "png",
        "scale": "device",
        "animations": "disabled",
        "caret": "hide",
        "timeout": 30000,
    }
    tweet = (context or {}).get("tweet") or {}
    has_article = tweet.get("article") or (tweet.get("quote") or {}).get("article")
    if context and not _tweet_has_media(context) and not has_article:
        options["clip"] = {
            "x": 0,
            "y": 0,
            "width": int(TWEET_CARD_WIDTH * TWEET_CARD_RENDER_SCALE),
            "height": estimate_tweet_card_height(context),
        }
    else:
        options["full_page"] = True
    return options


def _tweet_has_media(context: dict) -> bool:
    tweet = (context or {}).get("tweet") or {}
    if tweet.get("media"):
        return True
    quote = tweet.get("quote") or {}
    return bool(quote.get("media"))


def _display_units(text: str) -> int:
    units = 0
    for char in text:
        units += 2 if unicodedata.east_asian_width(char) in ("F", "W", "A") else 1
    return units


def _line_count(text: str, units_per_line: int) -> int:
    if not text:
        return 0
    lines = 0
    for part in str(text).splitlines() or [""]:
        lines += max(1, math.ceil(_display_units(part) / units_per_line))
    return lines


def _media_logical_height(media: list[dict], is_quote: bool = False) -> int:
    count = len(media or [])
    if count <= 0:
        return 0
    if is_quote:
        return 210 if count <= 2 else 422
    return 360 if count == 1 else 502 if count >= 3 else 250


def _estimate_quote_logical_height(quote: dict) -> int:
    if not quote:
        return 0

    height = 14 + 24 + 22
    quote_lines = _line_count(str(quote.get("text") or ""), 62)
    if quote_lines:
        height += 8 + quote_lines * 24

    media_height = _media_logical_height(quote.get("media") or [], is_quote=True)
    if media_height:
        height += 12 + media_height
    return height


def estimate_tweet_card_height(context: dict) -> int:
    """估算卡片渲染高度，用于裁剪无媒体短推文。"""
    tweet = (context or {}).get("tweet") or {}

    main_height = 44
    text_lines = _line_count(str(tweet.get("text") or ""), 60)
    if text_lines:
        main_height += 2 + text_lines * 27

    if tweet.get("translation_note"):
        main_height += 10 + 21
    if tweet.get("date"):
        main_height += 18 + 22

    quote_height = _estimate_quote_logical_height(tweet.get("quote") or {})
    if quote_height:
        main_height += quote_height

    media_height = _media_logical_height(tweet.get("media") or [], is_quote=False)
    if media_height:
        main_height += 12 + media_height

    main_height += 12 + 24 + 12

    logical_height = 38 + 2 + max(48, main_height)
    if tweet.get("retweet_label"):
        logical_height += 26

    # 留少量余量，兼容字形下沿、边框、emoji 回退和换行差异。
    return math.ceil((logical_height + 12) * TWEET_CARD_RENDER_SCALE)


def _text(value) -> str:
    return str(value or "").strip()


def _handle(value) -> str:
    return _text(value).lstrip("@")


def _media_class(count: int) -> str:
    if count <= 1:
        return "one"
    if count == 2:
        return "two"
    if count == 3:
        return "three"
    return "four"


def _media_items(images: list, video_previews: list, videos: list) -> list[dict]:
    items: list[dict] = []
    for image in images or []:
        url = _text(image)
        if url:
            items.append({"kind": "image", "url": url, "duration": ""})

    for preview in video_previews or []:
        if isinstance(preview, dict):
            url = _text(preview.get("poster") or preview.get("url"))
            duration = _text(preview.get("duration"))
        else:
            url = _text(preview)
            duration = ""
        items.append({"kind": "video", "url": url, "duration": duration})

    if videos and not video_previews:
        items.append({"kind": "video", "url": "", "duration": ""})

    return items[:4]


def _actions(stats: dict) -> list[dict]:
    stats = stats or {}
    return [
        {"label": "回复", "icon": "reply", "count": _text(stats.get("comments"))},
        {"label": "转发", "icon": "repost", "count": _text(stats.get("retweets"))},
        {"label": "喜欢", "icon": "like", "count": _text(stats.get("likes"))},
        {"label": "查看", "icon": "views", "count": _text(stats.get("views"))},
    ]


def _article_context(tweet: dict, translated: bool = False) -> dict | None:
    article = tweet.get("translated_article") if translated else None
    return article or tweet.get("article") or None


def _article_media_urls(article: dict | None) -> set[str]:
    if not article:
        return set()
    return {article.get("cover") or ""} | {
        b.get(field) or "" for b in article.get("blocks") or []
        if b.get("type") in ("image", "video") for field in ("url", "poster")
    }


def build_tweet_card_context(
    username: str,
    tweet_info: dict,
    translated_text: str | None = None,
    translate_model: str | None = None,
    theme: str = "dark",
) -> dict:
    """构建 html_render 使用的 Jinja 上下文。"""
    theme = _text(theme).lower()
    if theme not in ("dark", "light"):
        theme = "dark"
    author_username = _handle(tweet_info.get("username") or username)
    screen_name = _text(tweet_info.get("screen_name") or author_username)
    text = _text(
        translated_text if translated_text is not None else tweet_info.get("text")
    )
    quote = tweet_info.get("quote") or None
    retweet = tweet_info.get("retweet") or None
    article = _article_context(tweet_info, translated_text is not None)
    if article:
        text = _text(tweet_info.get("translated_article_lead") if translated_text is not None
                     else tweet_info.get("article_lead"))
    article_urls = _article_media_urls(article)
    media = _media_items(
        [u for u in tweet_info.get("images") or [] if u not in article_urls],
        [p for p in tweet_info.get("video_previews") or [] if (p.get("poster") if isinstance(p, dict) else p) not in article_urls],
        [u for u in tweet_info.get("videos") or [] if u not in article_urls],
    )

    retweet_label = ""
    if retweet:
        retweeter_name = _text(
            retweet.get("retweeter_screen_name") or retweet.get("retweeter_username")
        )
        retweeter_username = _handle(retweet.get("retweeter_username"))
        if retweeter_name and retweeter_username:
            retweet_label = f"{retweeter_name} @{retweeter_username} 已转帖"
        elif retweeter_name or retweeter_username:
            retweet_label = f"{retweeter_name or ('@' + retweeter_username)} 已转帖"

    quote_context = None
    if quote:
        quote_username = _handle(quote.get("username"))
        quote_name = _text(quote.get("author") or quote_username)
        quote_article = _article_context(quote, bool(quote.get("translated_text")))
        quote_article_urls = _article_media_urls(quote_article)
        quote_media = _media_items(
            [u for u in quote.get("images") or [] if u not in quote_article_urls],
            [p for p in quote.get("video_previews") or [] if (p.get("poster") if isinstance(p, dict) else p) not in quote_article_urls],
            [u for u in quote.get("videos") or [] if u not in quote_article_urls],
        )
        quote_context = {
            "name": quote_name or quote_username,
            "username": quote_username,
            "avatar": _text(quote.get("avatar")),
            "verified": bool(quote.get("verified")),
            "date": _text(quote.get("date")),
            "text": _text(quote.get("translated_text") or quote.get("text")),
            "article": quote_article,
            "media": quote_media,
            "media_class": _media_class(len(quote_media)),
        }
        if quote_article:
            quote_context["text"] = _text(quote.get("translated_article_lead")
                if quote.get("translated_text") else quote.get("article_lead"))

    quote_translated = bool((quote or {}).get("translated_text"))
    translation_note = ""
    if translate_model and (translated_text is not None or quote_translated):
        translation_note = f"由 {translate_model} 翻译自原文"

    return {
        "theme": theme,
        "render_scale": TWEET_CARD_RENDER_SCALE,
        "tweet": {
            "name": screen_name or author_username,
            "username": author_username,
            "avatar": _text(tweet_info.get("avatar")),
            "verified": bool(tweet_info.get("verified")),
            "date": _text(tweet_info.get("date")),
            "text": text,
            "article": article,
            "translation_note": translation_note,
            "retweet_label": retweet_label,
            "quote": quote_context,
            "media": media,
            "media_class": _media_class(len(media)),
            "actions": _actions(tweet_info.get("stats") or {}),
        }
    }
