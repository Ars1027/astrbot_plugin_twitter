import asyncio
from dataclasses import replace

import pytest

from support.fakes import Video, TG_GROUP, TG_TOPIC, make_event

MB = 1024 * 1024
MAIN_VIDEO = "https://media.test/main.mp4"
QUOTE_VIDEO = "https://media.test/quote.mp4"


def tweet():
    return {
        "status": True, "tweet_id": "101", "username": "tester", "text": "正文",
        "videos": [MAIN_VIDEO],
        "quote": {"username": "quoted", "text": "引用", "videos": [QUOTE_VIDEO]},
    }


async def prepare(plugin, mode="text"):
    plugin.message_service.settings = replace(plugin.message_service.settings, text_render_mode=mode)

    async def size(_url):
        return 80 * MB

    async def render(*_args, **_kwargs):
        if mode == "render_failed":
            raise RuntimeError("render failed")
        return "https://render.test/card.png"

    async def album(*_args, **_kwargs):
        return True

    plugin.twitter_api.get_remote_file_size = size
    plugin.message_service.html_render = render
    plugin.delivery_service.telegram_albums.send = album
    if mode == "render_failed":
        plugin.message_service.settings = replace(plugin.message_service.settings, text_render_mode="screenshot")


@pytest.mark.parametrize("config,expected", [
    ({}, 50),
    ({"twitter_video_max_size_mb": 20}, 50),
    ({"twitter_telegram_video_max_size_mb": 100}, 100),
    ({"message_format": {"twitter_telegram_video_max_size_mb": 2000}}, 2000),
    ({"message_format": {"twitter_telegram_video_max_size_mb": 0}}, 1),
])
def test_telegram_limit_config_is_independent_and_compatible(plugin_env, config, expected):
    module, make, _data, _calls, _fail = plugin_env
    plugin = module.TwitterPlugin(make().context, config)
    assert plugin.telegram_video_max_size_mb == expected
    assert plugin.delivery_service.settings.telegram_video_max_size_mb == expected
    assert plugin.video_max_size_mb == config.get("twitter_video_max_size_mb", 256)
    assert plugin.message_service.settings.video_max_size_mb == plugin.video_max_size_mb


@pytest.mark.asyncio
@pytest.mark.parametrize("collective", [False, True])
@pytest.mark.parametrize("mode", ["text", "screenshot", "render_failed"])
async def test_mixed_qq_and_telegram_delivery_use_separate_limits(plugin_env, collective, mode):
    _module, make, data, calls, _fail = plugin_env
    plugin = make(collective=collective)
    await prepare(plugin, mode)
    qq = "qq-bot:GroupMessage:456"
    data["twitter_subs"] = {"tester": {"subscribers": {
        TG_GROUP: {"status": True}, qq: {"status": True},
    }}}
    # 验证不同平台并发发送不会修改共享消息服务的配置。
    if collective:
        await plugin.delivery_service.push_to_subscribers("tester", tweet())
        result = await plugin.delivery_service.flush_collected()
        assert not result.failed_authors
    else:
        results = await asyncio.gather(*[
            plugin.delivery_service.send_to_subscriber(umo, "tester", tweet(), {}, "Tester")
            for umo in (TG_GROUP, qq)
        ])
        assert all(results)
    assert not any(isinstance(part, Video) for umo, chain in calls if umo == TG_GROUP for part in chain)
    assert sum(isinstance(part, Video) for umo, chain in calls if umo == qq for part in chain) == 2
    assert plugin.message_service.settings.video_max_size_mb == 256
    if mode != "screenshot":
        tg_text = "\n".join(getattr(part, "text", "") for umo, chain in calls if umo == TG_GROUP for part in chain)
        assert "超过 50 MB" in tg_text and MAIN_VIDEO in tg_text and QUOTE_VIDEO in tg_text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["test", "parse"])
@pytest.mark.parametrize("limit,expected_videos", [(50, 0), (100, 2)])
async def test_telegram_commands_use_configured_limit(plugin_env, command, limit, expected_videos):
    module, make, _data, calls, _fail = plugin_env
    plugin = module.TwitterPlugin(make().context, {"message_format": {
        "twitter_telegram_video_max_size_mb": limit,
    }})
    plugin._provider_ready = True
    await prepare(plugin)

    async def timeline(*_args, **_kwargs):
        return [{"tweet_id": "101", "username": "tester"}]

    async def detail(*_args, **_kwargs):
        return tweet()

    plugin.twitter_api.get_user_timeline_items = timeline
    plugin.twitter_api.get_tweet = detail
    event = make_event(TG_TOPIC, "https://x.com/tester/status/101")
    if command == "test":
        _results = [item async for item in plugin.test_tweet(event, "tester")]
    else:
        match = module.TWITTER_LINK_PATTERN.search(event.message_str)
        _results = [item async for item in plugin._handle_tweet_link(event, match, report_errors=True)]
    assert sum(isinstance(part, Video) for _umo, chain in calls for part in chain) == expected_videos
    if not expected_videos:
        text = "\n".join(getattr(part, "text", "") for _umo, chain in calls for part in chain)
        assert "超过 50 MB" in text and MAIN_VIDEO in text and QUOTE_VIDEO in text


@pytest.mark.asyncio
@pytest.mark.parametrize("size,expected", [(50 * MB, False), (50 * MB + 1, True), (None, False)])
async def test_telegram_size_boundary_and_unknown_size(plugin_env, size, expected):
    _module, make, _data, _calls, _fail = plugin_env
    service = make().message_service

    async def get_size(_url):
        return size

    service.twitter_api.get_remote_file_size = get_size
    exceeded, _bytes = await service.video_exceeds_size_limit(MAIN_VIDEO, video_max_size_mb=50)
    assert exceeded is expected


@pytest.mark.asyncio
async def test_telegram_disabled_media_and_streams_keep_existing_behavior(plugin_env):
    _module, make, _data, _calls, _fail = plugin_env
    service = make().message_service

    async def no_probe(_url):
        pytest.fail("关闭媒体或流媒体不应探测大小")

    service.twitter_api.get_remote_file_size = no_probe
    exceeded, _size = await service.video_exceeds_size_limit("https://media.test/video.m3u8", video_max_size_mb=50)
    assert not exceeded
    service.settings = replace(service.settings, send_media_separately=False)
    chain = await service.build_message_chain("tester", tweet(), video_max_size_mb=50)
    assert not any(isinstance(part, Video) for part in chain)
    assert MAIN_VIDEO not in "".join(getattr(part, "text", "") for part in chain)
