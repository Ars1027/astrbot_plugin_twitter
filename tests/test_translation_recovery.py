"""模拟 Provider 阻塞，验证翻译预算和轮询自愈，不调用真实 LLM。"""

import asyncio
import copy
import sys
import types

import pytest

from test_media_lifecycle import (
    Nodes,
    Plain,
    _delivery_settings,
    _message_settings,
    plugin_module as plugin_module,
)


class Context:
    def __init__(self):
        self.calls = []
        self.cancelled = 0
        self.entered = asyncio.Event()
        self.provider = "provider-a"
        self.block = True

    def get_provider_by_id(self, _provider_id):
        return types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(model_name="model")
        )

    async def get_current_chat_provider_id(self, umo):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled += 1
        return types.SimpleNamespace(completion_text="译文")


def make_service(plugin_module, context, **settings):
    return plugin_module.TweetMessageService(
        context,
        object(),
        None,
        _message_settings(
            plugin_module,
            translate_enabled=True,
            translate_timeout_seconds=0.04,
            **settings,
        ),
    )


def cycle_type(plugin_module):
    return sys.modules[
        f"{plugin_module.__package__}.services.tweet_message_service"
    ].TranslationCycleState


@pytest.mark.asyncio
async def test_timeout_cancels_llm_and_clears_old_quote_translation(plugin_module):
    context = Context()
    service = make_service(plugin_module, context)
    tweet = {"text": "原文", "quote": {"text": "引用原文", "translated_text": "旧译文"}}
    tasks_before = asyncio.all_tasks()
    assert await service.maybe_translate(tweet, "session") == (None, None)
    assert context.cancelled == 1
    assert "translated_text" not in tweet["quote"]
    assert asyncio.all_tasks() == tasks_before
    chain = await service.build_tweet_chain("author", tweet)
    assert "原文" in chain[0].text and "引用原文" in chain[0].text
    assert "翻译自原文" not in chain[0].text


@pytest.mark.asyncio
async def test_main_success_survives_quote_timeout_with_one_shared_budget(
    plugin_module, monkeypatch
):
    context = Context()
    service = make_service(plugin_module, context)
    original_generate = context.llm_generate
    original_wait_for = asyncio.wait_for
    budgets = []

    async def generate(**kwargs):
        if kwargs["prompt"] == "正文":
            return types.SimpleNamespace(completion_text="正文译文")
        return await original_generate(**kwargs)

    async def wait_for(awaitable, timeout):
        budgets.append(timeout)
        return await original_wait_for(awaitable, timeout)

    context.llm_generate = generate
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    tweet = {"text": "正文", "quote": {"text": "引用"}}
    assert await service.maybe_translate(tweet, "session") == ("正文译文", "model")
    assert budgets == [0.04]
    assert "translated_text" not in tweet["quote"]
    assert context.cancelled == 1


@pytest.mark.asyncio
async def test_provider_selection_and_retry_sleep_share_timeout(plugin_module):
    context = Context()
    service = make_service(plugin_module, context)
    cancelled = []

    async def select(umo):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    context.get_current_chat_provider_id = select
    state = cycle_type(plugin_module)()
    assert await service.maybe_translate({"text": "正文"}, "session", state) == (
        None,
        None,
    )
    assert cancelled == [True] and not context.calls and not state.failures

    async def select_fast(umo):
        return "provider-a"

    async def empty_response(**kwargs):
        context.calls.append(kwargs)
        return types.SimpleNamespace(completion_text=" ")

    context.get_current_chat_provider_id = select_fast
    context.llm_generate = empty_response
    assert await service.maybe_translate({"text": "正文"}, "session", state) == (
        None,
        None,
    )
    assert len(context.calls) == 1  # 重试等待同样受总时限约束。
    assert state.failures == {"provider-a": 1}


@pytest.mark.asyncio
async def test_cycle_skips_after_two_failures_but_manual_and_new_cycle_recover(
    plugin_module,
):
    context = Context()
    service = make_service(plugin_module, context)
    state = cycle_type(plugin_module)()
    for _ in range(3):
        assert await service.maybe_translate({"text": "正文"}, "session", state) == (
            None,
            None,
        )
    assert len(context.calls) == 2
    assert state.failures == {"provider-a": 2} and state.skipped == 1

    context.block = False
    assert await service.maybe_translate({"text": "正文"}, "session") == (
        "译文",
        "model",
    )
    assert state.failures == {"provider-a": 2}
    fresh = cycle_type(plugin_module)()
    assert await service.maybe_translate({"text": "正文"}, "session", fresh) == (
        "译文",
        "model",
    )
    assert fresh.failures == {"provider-a": 0}
    context.provider = "provider-b"
    assert await service.maybe_translate({"text": "正文"}, "session", state) == (
        "译文",
        "model",
    )
    assert state.failures == {"provider-a": 2, "provider-b": 0}


@pytest.mark.asyncio
async def test_success_resets_counter_and_empty_tweets_do_not_count(plugin_module):
    context = Context()
    service = make_service(plugin_module, context)
    state = cycle_type(plugin_module)()
    await service.maybe_translate({"text": "正文"}, "session", state)
    await service.maybe_translate({"text": " "}, "session", state)
    assert state.failures == {"provider-a": 1}
    context.block = False
    await service.maybe_translate(
        {"text": "正文", "quote": {"text": "引用"}}, "session", state
    )
    assert state.failures == {"provider-a": 0}
    context.block = True
    await service.maybe_translate({"text": "正文"}, "session", state)
    assert state.failures == {"provider-a": 1}


@pytest.mark.asyncio
async def test_disabled_or_missing_provider_does_not_count(plugin_module):
    context = Context()
    state = cycle_type(plugin_module)()
    disabled = plugin_module.TweetMessageService(
        context, object(), None, _message_settings(plugin_module)
    )
    tweet = {"text": "正文", "quote": {"translated_text": "旧译文"}}
    assert await disabled.maybe_translate(tweet, "session", state) == (None, None)
    assert "translated_text" not in tweet["quote"]
    service = make_service(plugin_module, context)
    context.provider = None
    context.get_all_providers = lambda: []
    assert await service.maybe_translate(tweet, "session", state) == (None, None)
    assert not state.failures and not context.calls


@pytest.mark.asyncio
async def test_quote_only_translation_and_external_cancel(plugin_module):
    context = Context()
    context.block = False
    service = make_service(plugin_module, context)
    tweet = {"quote": {"text": "引用原文"}}
    assert await service.maybe_translate(tweet, "session") == (None, "model")
    assert tweet["quote"]["translated_text"] == "译文"
    context.block = True
    context.entered.clear()
    state = cycle_type(plugin_module)()
    task = asyncio.create_task(
        service.maybe_translate({"text": "正文"}, "session", state)
    )
    await context.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.cancelled == 1 and not state.failures


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_part", ["正文", "引用"])
async def test_partial_failure_counts_once_and_keeps_successful_part(
    plugin_module, monkeypatch, failing_part
):
    context = Context()
    service = make_service(
        plugin_module,
        context,
        translate_custom_prompt_enabled=True,
        translate_custom_prompt="翻译为{target_lang}",
    )
    module = sys.modules[f"{plugin_module.__package__}.services.tweet_message_service"]

    async def no_delay(_seconds):
        pass

    async def generate(**kwargs):
        context.calls.append(kwargs)
        if kwargs["prompt"] == failing_part:
            raise ConnectionError("offline")
        return types.SimpleNamespace(completion_text="译文")

    monkeypatch.setattr(
        module,
        "asyncio",
        types.SimpleNamespace(
            wait_for=asyncio.wait_for,
            get_running_loop=asyncio.get_running_loop,
            sleep=no_delay,
        ),
    )
    context.llm_generate = generate
    state = cycle_type(plugin_module)()
    tweet = {"text": "正文", "quote": {"text": "引用"}}
    text, model = await service.maybe_translate(tweet, "session", state)
    assert state.failures == {"provider-a": 1}
    assert len(context.calls) == 3
    assert all(call["system_prompt"] == "翻译为简体中文" for call in context.calls)
    assert model == "model"
    assert (text is None) == (failing_part == "正文")
    assert ("translated_text" in tweet["quote"]) == (failing_part == "正文")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["nitter", "fxtwitter"])
@pytest.mark.parametrize("collective", [False, True])
async def test_blocked_translation_still_delivers_and_commits_cursors(
    plugin_module, monkeypatch, provider, collective
):
    store = {
        "twitter_subs": {
            user: {
                "screen_name": user,
                "since_id": "0",
                "subscribers": {
                    "session": {"status": True, "r18": True, "media": False}
                },
            }
            for user in ("author-a", "author-b")
        }
    }
    checkpoints = []
    context = Context()
    sent_text = []

    async def send_message(_umo, chain):
        components = chain.chain
        if isinstance(components[0], Nodes):
            components = [part for node in components[0].nodes for part in node.content]
        sent_text.extend(part.text for part in components if isinstance(part, Plain))

    context.send_message = send_message

    class API:
        is_ready = True

        async def get_user_timeline_items(self, username, since_id):
            return [
                {"tweet_id": str(i), "username": username}
                for i in range(int(since_id) + 1, 4)
            ]

        async def get_tweet(self, username, tweet_id):
            return {
                "tweet_id": tweet_id,
                "username": username,
                "text": f"正文{tweet_id}",
            }

    async def get_kv(key, default):
        return copy.deepcopy(store.get(key, default))

    async def put_kv(key, data):
        previous = store.get(key, {})
        store[key] = copy.deepcopy(data)
        if key == "twitter_subs":
            assert sent_text  # 原文送达之后才能保存处理结果。
            # History-only writes are not cursor checkpoints.
            if any(info["since_id"] != previous[user]["since_id"]
                   for user, info in data.items()):
                checkpoints.append(copy.deepcopy(data))

    async def no_delay(_seconds):
        pass

    polling_module = sys.modules[
        f"{plugin_module.__package__}.services.polling_service"
    ]
    monkeypatch.setattr(
        polling_module, "asyncio", types.SimpleNamespace(sleep=no_delay)
    )
    api = API()
    subscriptions = plugin_module.SubscriptionService(get_kv, put_kv, api, lambda: True)
    messages = make_service(plugin_module, context, text_render_mode="text")
    delivery = plugin_module.TweetDeliveryService(
        context,
        subscriptions,
        messages,
        _delivery_settings(
            plugin_module, use_node=collective, collective_forward=collective
        ),
    )
    polling = plugin_module.PollingService(
        api,
        subscriptions,
        delivery,
        plugin_module.PollingSettings(True, provider, "https://nitter.test", ()),
    )
    await asyncio.wait_for(polling.check_all(), 2)
    assert len(context.calls) == context.cancelled == 2
    assert len(sent_text) == 6
    assert all("翻译自原文" not in text for text in sent_text)
    assert all(author["since_id"] == "3" for author in store["twitter_subs"].values())
    assert len(checkpoints) == (2 if collective else 6)
    for author in store["twitter_subs"].values():
        history = author["subscribers"]["session"]["recent_deliveries"]
        assert [item["tweet_id"] for item in history] == ["3", "2", "1"]
    assert not delivery.has_collected and not polling.has_pending_collective

    # 下一轮重新建立失败计数，不沿用上一轮的跳过状态。
    for author in store["twitter_subs"].values():
        author["since_id"] = "0"
        author["processed_tweet_ids"] = []
    context.block = False
    await polling.check_all()
    assert len(context.calls) == 8
