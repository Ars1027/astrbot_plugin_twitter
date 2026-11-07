import asyncio
import re
import sys

import pytest
from test_telegram_support import TG_GROUP, TG_PRIVATE
from test_telegram_support import plugin_env as plugin_env  # noqa: PLC0414


@pytest.fixture
def audit_env(plugin_env, monkeypatch):
    _module, make, data, _calls, _fail = plugin_env
    plugin = make()
    module = sys.modules[type(plugin.subscription_service).__module__]
    logs = {level: [] for level in ("debug", "info", "warning")}
    for level, lines in logs.items():
        monkeypatch.setattr(module.logger, level, lines.append)

    async def profile(_username):
        return {"status": True, "screen_name": "Tester", "bio": "secret biography"}

    async def timeline(_username):
        return ["100"]

    plugin.twitter_api.get_user_info = profile
    plugin.twitter_api.get_user_newtimeline = timeline
    return plugin, data, logs


@pytest.mark.asyncio
async def test_new_subscription_logs_selected_and_saved_cursor_after_commit(audit_env):
    plugin, data, logs = audit_env
    service = plugin.subscription_service
    result = await service.add(TG_GROUP, "@tester", r18=True, media_only=True)
    assert result["ok"] and result["created_author"]
    assert data["twitter_subs"]["tester"]["since_id"] == "100"
    selected, saved, outcome = logs["info"]
    assert "subscription.cursor.selected" in selected and 'since="100"' in selected
    assert "subscription.cursor.saved" in saved and 'source="new"' in saved
    assert "r18=true" in saved and "media_only=true" in saved
    assert "subscription.add.result" in outcome and "created_author=true" in outcome
    assert len({re.search(r"trace=(\w+)", line)[1] for line in logs["info"] + logs["debug"]}) == 1
    assert "trace=-" not in outcome
    assert TG_GROUP not in "\n".join(logs["info"])
    assert "secret" not in "\n".join(logs["info"])


@pytest.mark.asyncio
async def test_existing_subscription_logs_reused_cursor_and_rejected_duplicate(audit_env):
    plugin, data, logs = audit_env
    data["twitter_subs"] = {"tester": {"since_id": "200", "subscribers": {TG_GROUP: {}}}}

    async def unexpected(_username):
        pytest.fail("已有推主不应重新获取初始游标")

    plugin.twitter_api.get_user_newtimeline = unexpected
    service = plugin.subscription_service
    result = await service.add(TG_PRIVATE, "TESTER")
    assert result["ok"] and not result["created_author"]
    assert 'source="existing"' in logs["info"][0] and 'since="200"' in logs["info"][0]
    assert "replaced=false" in logs["info"][1]
    await service.add(TG_PRIVATE, "tester")
    assert "replaced=true" in logs["info"][-1]
    before = len(logs["info"])
    result = await service.add(TG_PRIVATE, "tester", reject_duplicate=True)
    assert not result["ok"]
    assert len(logs["info"]) == before + 1
    assert 'reason="duplicate"' in logs["info"][-1] and "ok=false" in logs["info"][-1]


@pytest.mark.asyncio
async def test_concurrent_add_reports_final_shared_cursor_instead_of_candidate(audit_env):
    plugin, data, logs = audit_env

    async def timeline(_username):
        data["twitter_subs"] = {"tester": {"since_id": "300", "subscribers": {TG_PRIVATE: {}}}}
        return ["100"]

    plugin.twitter_api.get_user_newtimeline = timeline
    result = await plugin.subscription_service.add(TG_GROUP, "tester")
    assert not result["created_author"]
    assert 'since="100"' in logs["info"][0]
    assert 'since="300"' in logs["info"][1] and 'source="concurrent_existing"' in logs["info"][1]
    assert data["twitter_subs"]["tester"]["since_id"] == "300"


@pytest.mark.asyncio
async def test_remove_distinguishes_relation_removal_author_removal_and_missing(audit_env):
    plugin, data, logs = audit_env
    data["twitter_subs"] = {"tester": {"since_id": "200", "subscribers": {TG_GROUP: {}, TG_PRIVATE: {}}}}
    service = plugin.subscription_service
    await service.remove(TG_GROUP, "tester")
    assert "author_removed=false" in logs["info"][-1]
    assert data["twitter_subs"]["tester"]["since_id"] == "200"
    await service.remove(TG_PRIVATE, "tester")
    assert "author_removed=true" in logs["info"][-1] and not data["twitter_subs"]
    await service.remove(TG_PRIVATE, "tester")
    assert 'reason="not_found"' in logs["info"][-1] and "ok=false" in logs["info"][-1]
    assert all("subscription.remove.result" in line for line in logs["info"])


@pytest.mark.asyncio
async def test_update_session_toggle_and_clear_log_only_safe_fields(audit_env):
    plugin, data, logs = audit_env
    data["twitter_subs"] = {"tester": {"since_id": "200", "subscribers": {TG_GROUP: {}, TG_PRIVATE: {}}}}
    service = plugin.subscription_service
    await service.update(TG_GROUP, "tester", {"enabled": False, "r18": True, "unknown": "secret token"})
    assert "subscription.update.result" in logs["info"][-1]
    assert '"enabled": false' in logs["info"][-1] and '"r18": true' in logs["info"][-1]
    assert await service.set_session_status(TG_GROUP, True) == 1
    assert "subscription.session_status.result" in logs["info"][-1]
    assert "enabled=true" in logs["info"][-1] and "affected=1" in logs["info"][-1]
    await service.clear()
    assert "subscription.clear.result" in logs["info"][-1]
    assert "authors=1" in logs["info"][-1] and "relations=2" in logs["info"][-1]
    assert "secret" not in "\n".join(logs["info"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_save_never_logs_success(audit_env, error_type):
    plugin, _data, logs = audit_env

    async def fail(_key, _value):
        raise error_type("secret request and token")

    plugin.put_kv_data = fail
    with pytest.raises(error_type):
        await plugin.subscription_service.add(TG_GROUP, "tester")
    assert not any("subscription.cursor.saved" in line or "subscription.add.result" in line for line in logs["info"])
    assert len(logs["warning"]) == 1 and "subscription.add.failed" in logs["warning"][0]
    assert "secret" not in logs["warning"][0]
