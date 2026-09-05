"""可选的本地浏览器回归测试；不增加插件运行时依赖。"""

import base64
import io
import os
import sys
from pathlib import Path

import pytest

from test_media_lifecycle import ROOT, plugin_module as plugin_module


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            instance = driver.chromium.launch(
                channel=os.getenv("TWITTER_TEST_BROWSER_CHANNEL") or None,
                headless=True,
            )
        except playwright.Error as exc:
            pytest.skip(f"未安装可用的测试浏览器: {exc}")
        yield instance
        instance.close()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("avatar_state", ["cached", "missing", "decode-error"])
def test_avatar_rendering_has_no_broken_images_or_layout_changes(
    plugin_module, browser, tmp_path, theme, avatar_state
):
    jinja = pytest.importorskip("jinja2")
    pil = pytest.importorskip("PIL.Image")
    renderer = sys.modules[f"{plugin_module.__package__}.twitter_renderer"]
    uri = (
        "data:image/png;base64,"
        + base64.b64encode((ROOT / "logo.png").read_bytes()).decode()
    )
    avatar = (
        uri
        if avatar_state == "cached"
        else (
            "data:image/png;base64,iVBORw0KGgo="
            if avatar_state == "decode-error"
            else None
        )
    )
    tweet = {
        "username": "author",
        "screen_name": "推文作者",
        "text": "正文第一行\n正文第二行",
        "avatar": avatar,
        "verified": True,
        "quote": {
            "username": "quoted",
            "author": "引用作者",
            "text": "引用正文",
            "avatar": avatar,
        },
    }
    context = renderer.build_tweet_card_context("author", tweet, theme=theme)
    template = jinja.Template(renderer.load_tweet_card_template())
    page = browser.new_page(
        viewport={"width": 1280, "height": 720}, device_scale_factor=1
    )
    try:
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.set_content(template.render(**context))
        page.evaluate("""async () => {
          await document.fonts.ready;
          await Promise.all([...document.images].map(img => img.decode().catch(() => {})));
        }""")
        frames = page.locator(".avatar-frame")
        assert frames.count() == 2
        assert frames.nth(0).bounding_box()["width"] == 72
        assert frames.nth(1).bounding_box()["width"] == 36
        assert page.locator(".shot").bounding_box()["width"] == 1140
        visible_images = page.locator(".avatar-frame img:visible")
        assert visible_images.count() == (2 if avatar_state == "cached" else 0)
        assert all(
            visible_images.nth(i).evaluate("img => img.naturalWidth > 0")
            for i in range(visible_images.count())
        )
        assert not requests
        assert page.evaluate("""() => {
          const card = document.querySelector('.shot').getBoundingClientRect();
          return [...document.querySelectorAll('.avatar-frame, .tweet-text, .quote-text')]
            .every(el => { const r = el.getBoundingClientRect();
              return r.left >= card.left && r.right <= card.right && r.bottom <= card.bottom; });
        }""")
        image_bytes = page.locator(".shot").screenshot()
        with pil.open(io.BytesIO(image_bytes)) as image:
            assert image.width == 1140
            assert len(image.convert("RGB").getcolors(image.width * image.height)) > 10
            assert image.convert("RGB").getpixel((114, 110)) == (
                (0, 0, 0) if theme == "dark" else (255, 255, 255)
            )
        output = Path(os.getenv("TWITTER_VISUAL_OUTPUT_DIR", str(tmp_path)))
        output.mkdir(parents=True, exist_ok=True)
        (output / f"{theme}-{avatar_state}.png").write_bytes(image_bytes)
        height = page.locator(".shot").bounding_box()["height"]
        # 用同一模板、同一文本对比缓存头像和占位，尺寸不能因加载结果变化。
        context["tweet"]["avatar"] = uri if avatar_state != "cached" else None
        context["tweet"]["quote"]["avatar"] = context["tweet"]["avatar"]
        page.set_content(template.render(**context))
        assert page.locator(".shot").bounding_box()["height"] == height
    finally:
        page.close()
