"""将两个数据源的文章转换为正文块，供消息与截图共同使用。"""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag


def safe_url(value: str) -> str:
    value = str(value or "").strip()
    return value if urlsplit(value).scheme in ("http", "https") else ""


def text_block(text: str, kind: str = "paragraph", spans: list | None = None) -> dict:
    spans = spans or [{"text": text}]
    links = list(dict.fromkeys(s["href"] for s in spans if s.get("href")))
    return {"type": kind, "text": text, "spans": spans, "links": links}


def article_text(article: dict, lead: str = "") -> str:
    sections = [lead, f"【文章】{article.get('title') or ''}"]
    blocks = article.get("blocks") or []
    if not blocks:
        sections.append("（文章摘要，未获取到全文）")
        sections.append(article.get("preview_text") or "")
    number = 0
    for block in blocks:
        kind, text = block.get("type"), block.get("text") or ""
        links = [url for url in block.get("links") or [] if url not in text]
        if links:
            text += "\n" + "\n".join(links)
        if kind == "ordered-list-item":
            number += 1
            text = f"{number}. {text}"
        else:
            number = 0
            if kind == "unordered-list-item":
                text = f"• {text}"
            elif kind == "blockquote":
                text = f"> {text}"
        if kind == "video" and block.get("url"):
            text = text or "[视频]"
        if text:
            sections.append(text)
    if article.get("url"):
        sections.append(article["url"])
    return "\n\n".join(str(part).strip() for part in sections if part)


def attach_article(tweet: dict, article: dict | None) -> None:
    if not article:
        return
    lead = str(tweet.get("text") or "")
    # 只移除文章自身的入口，保留附言及其他链接。
    article_id = re.search(r"/i/article/(\d+)$", str(article.get("url") or ""))
    if article_id:
        lead = re.sub(
            rf"https?://[^\s]+/(?:i/article|[^/\s]+/article)/{article_id.group(1)}"
            r"(?:[?#]\S*)?(?=\s|$)",
            "", lead,
        )
    lead = lead.strip()
    tweet["article_lead"] = lead
    tweet["article"] = article
    tweet["text"] = article_text(article, lead)
    images = tweet.setdefault("images", [])
    videos = tweet.setdefault("videos", [])
    previews = tweet.setdefault("video_previews", [])
    for block in [{"type": "image", "url": article.get("cover")}] + article.get(
        "blocks", []
    ):
        url = block.get("url")
        if block.get("type") == "image" and url and url not in images:
            images.append(url)
        elif block.get("type") == "video":
            if url and url not in videos:
                videos.append(url)
            if block.get("poster") and not any(
                p.get("poster") == block["poster"] for p in previews
            ):
                previews.append({"poster": block["poster"], "duration": ""})


def _fx_spans(block: dict, entities: dict) -> list[dict]:
    text = str(block.get("text") or "")
    # Draft.js 使用 UTF-16 偏移；Python 字符索引不能直接套用。
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    styles = block.get("inlineStyleRanges") or []
    spans = []
    for index, char in enumerate(text):
        active = {
            str(s.get("style")).lower()
            for s in styles
            if isinstance(s, dict)
            and isinstance(s.get("offset"), int)
            and isinstance(s.get("length"), int)
            and s["offset"] <= offsets[index] < s["offset"] + s["length"]
        }
        attrs = {
            key: True
            for key in ("bold", "italic", "underline", "strikethrough", "code")
            if key in active
        }
        for er in block.get("entityRanges") or []:
            if (isinstance(er, dict) and isinstance(er.get("offset"), int)
                    and isinstance(er.get("length"), int)
                    and er["offset"] <= offsets[index] < er["offset"] + er["length"]):
                entity = entities.get(str(er.get("key")), {})
                if entity.get("type") == "LINK":
                    href = safe_url((entity.get("data") or {}).get("url"))
                    if href:
                        attrs["href"] = href
        if spans and {k: v for k, v in spans[-1].items() if k != "text"} == attrs:
            spans[-1]["text"] += char
        else:
            spans.append({"text": char, **attrs})
    return spans


def parse_fxtwitter_article(data, image_url, video_url) -> dict | None:
    if not isinstance(data, dict) or not (
        data.get("title") or data.get("preview_text")
    ):
        return None
    content = data.get("content") or {}
    content = content if isinstance(content, dict) else {}
    entities = content.get("entityMap") or []
    if isinstance(entities, list):
        entities = {
            str(e.get("key")): e.get("value", {})
            for e in entities
            if isinstance(e, dict)
        }
    if not isinstance(entities, dict):
        entities = {}
    media = {
        str(m.get("media_id")): m.get("media_info", {})
        for m in data.get("media_entities") or []
        if isinstance(m, dict)
    }
    blocks = []
    for block in content.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "paragraph")
        if kind != "atomic":
            text = str(block.get("text") or "")
            if text:
                blocks.append(text_block(text, kind, _fx_spans(block, entities)))
            continue
        for er in block.get("entityRanges") or []:
            entity = (
                entities.get(str(er.get("key")), {}) if isinstance(er, dict) else {}
            )
            info = entity.get("data") or {}
            if entity.get("type") == "MARKDOWN":
                blocks.append(text_block(str(info.get("markdown") or ""), "code-block"))
            elif (
                entity.get("type") == "TWEET" and str(info.get("tweetId", "")).isdigit()
            ):
                blocks.append(text_block(f"https://x.com/i/status/{info['tweetId']}"))
            elif entity.get("type") == "MEDIA":
                for item in info.get("mediaItems") or []:
                    m = media.get(str(item.get("mediaId")), {})
                    if m.get("__typename") == "ApiImage":
                        url = image_url(safe_url(m.get("original_img_url")))
                        if url:
                            blocks.append(
                                {
                                    "type": "image",
                                    "url": url,
                                    "text": str(info.get("caption") or ""),
                                }
                            )
                    else:
                        v = m.get("video_info") or m
                        url = video_url(
                            {
                                "formats": [
                                    {
                                        **variant,
                                        "bitrate": variant.get("bitrate")
                                        or variant.get("bit_rate")
                                        or 0,
                                    }
                                    for variant in v.get("variants") or []
                                    if isinstance(variant, dict)
                                ]
                            }
                        )
                        poster = image_url(safe_url(m.get("media_url_https")))
                        if url or poster:
                            blocks.append(
                                {
                                    "type": "video",
                                    "url": url,
                                    "poster": poster,
                                    "text": "",
                                }
                            )
    cover = data.get("cover_media") or {}
    cover = cover.get("media_info") or {} if isinstance(cover, dict) else {}
    article_id = str(data.get("id") or "")
    return {
        "title": str(data.get("title") or ""),
        "preview_text": str(data.get("preview_text") or ""),
        "url": f"https://x.com/i/article/{article_id}" if article_id.isdigit() else "",
        "cover": image_url(safe_url(cover.get("original_img_url"))),
        "blocks": blocks,
    }


def _html_spans(element: Tag, resolve_url) -> list[dict]:
    spans = []
    for node in element.descendants:
        if isinstance(node, NavigableString):
            attrs = {}
            for parent in node.parents:
                if parent is element.parent:
                    break
                key = {
                    "b": "bold",
                    "strong": "bold",
                    "i": "italic",
                    "em": "italic",
                    "u": "underline",
                    "del": "strikethrough",
                    "code": "code",
                }.get(parent.name)
                if key:
                    attrs[key] = True
                if parent.name == "a":
                    href = safe_url(resolve_url(parent.get("href", "")))
                    if href:
                        attrs["href"] = href
            spans.append({"text": str(node), **attrs})
        elif isinstance(node, Tag) and node.name == "br":
            spans.append({"text": "\n"})
    return spans


def parse_nitter_article(body: str, article: dict, resolve_image, resolve_url) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    main = soup.select_one("article.article-body")
    if not main:
        return article
    title = main.select_one(".article-title")
    if title:
        article["title"] = title.get_text(strip=True)
    cover = soup.select_one("img.article-cover")
    if cover:
        article["cover"] = resolve_image(cover) or article.get("cover", "")
    blocks = []
    for element in main.select(
        "p, li, h1, h2, h3, blockquote, pre, img, video, .timeline-item"
    ):
        if element.find_parent(
            class_="article-author"
        ) or "article-title" in element.get("class", []):
            continue
        if element.find_parent(["p", "li", "blockquote", "pre"]):
            continue
        if element.find_parent(class_="timeline-item"):
            continue
        if "timeline-item" in element.get("class", []):
            # Nitter renderTweet 的正文和引用仍跳过，仅保留该容器自身的入口。
            for link in element.select("a.tweet-link, a.unavailable-box, .tweet-date a"):
                if (link.find_parent(class_="timeline-item") is not element
                        or link.find_parent(class_="quote")):
                    continue
                match = re.fullmatch(
                    r"/(?:[A-Za-z0-9_]+|i)/status/(\d+)/?",
                    urlsplit(str(link.get("href") or "")).path,
                )
                if match:
                    blocks.append(text_block(f"https://x.com/i/status/{match.group(1)}"))
                    break
            continue
        if element.name == "img":
            url = resolve_image(element)
            if url:
                blocks.append(
                    {"type": "image", "url": url, "text": element.get("alt", "")}
                )
        elif element.name == "video":
            source = element.find("source")
            url = safe_url(
                resolve_url(
                    element.get("src") or (source.get("src") if source else "") or ""
                )
            )
            blocks.append(
                {
                    "type": "video",
                    "url": url,
                    "poster": resolve_url(element.get("poster", "")),
                    "text": "",
                }
            )
        else:
            spans = _html_spans(element, resolve_url)
            text = "".join(s["text"] for s in spans)
            kind = {
                "h1": "header-one",
                "h2": "header-two",
                "h3": "header-three",
                "blockquote": "blockquote",
                "pre": "code-block",
            }.get(element.name, "paragraph")
            if element.name == "li":
                kind = (
                    "ordered-list-item"
                    if element.parent.name == "ol"
                    else "unordered-list-item"
                )
            if text.strip():
                blocks.append(text_block(text, kind, spans))
    article["blocks"] = blocks
    return article
