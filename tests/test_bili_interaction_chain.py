"""Exercise account authorization and login recovery without network or QQ I/O."""

import ast
import asyncio
import json
import time
import uuid
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_bili_activity.core import (
    WRITES,
    Store,
    canonical,
    model_json,
)
from data.plugins.astrbot_plugin_bili_activity.interactions import (
    DEFAULTS,
    InteractionBlocked,
    Interactions,
    authorization_prompt,
    auto_allowed,
    enabled,
    safe_result,
)

SOURCE = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_bili_activity/main.py"


@pytest.fixture
def controller(tmp_path):
    """Bind deployed methods to temporary state and strictly fake transports."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    plugin = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BiliActivity"
    )
    names = {
        "refresh_interaction_login",
        "interaction_run",
        "propose",
        "automatic_proposal",
        "execute_interaction",
        "parse_json_argument",
        "interaction_status",
        "poll_replies",
    }
    methods = [
        n
        for n in plugin.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    parser = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "parse_json_argument"
    )
    for method in methods:
        method.decorator_list = []
    namespace = {
        "AstrMessageEvent": object,
        "asyncio": asyncio,
        "json": json,
        "time": time,
        "uuid": uuid,
        "authorization_prompt": authorization_prompt,
        "WRITES": WRITES,
        "InteractionBlocked": InteractionBlocked,
        "BudgetExceeded": type("BudgetExceeded", (RuntimeError,), {}),
        "interaction_enabled": enabled,
        "auto_allowed": auto_allowed,
        "safe_result": safe_result,
        "canonical": canonical,
        "model_json": model_json,
    }
    exec(
        compile(
            ast.Module(body=[parser, *methods], type_ignores=[]), str(SOURCE), "exec"
        ),
        namespace,
    )
    cfg = DEFAULTS | {
        "owner_scope": "qq:FriendMessage:123",
        "owner_id": "123",
        "public_interactions": False,
        "engagement_actions": True,
        "collection_management": True,
        "auto_collection_management": True,
        "interaction_mode": "auto_low_risk",
        "action_interval_ms": [0, 0],
    }
    store = Store(tmp_path)
    state = Interactions(store)
    calls = []
    login = {"logged_in": True, "uid": 10}

    async def rpc(name, payload):
        calls.append((name, payload))
        if name == "bili_read":
            assert payload == {"operation": "status", "args": {}}
            if isinstance(client.login, Exception):
                raise client.login
            return client.login
        if name == "bili_preview":
            return {
                "ok": True,
                "args": payload["args"] | {"account_uid": 10},
                "target": {"uid": payload["args"].get("uid"), "title": "test"},
            }
        if name == "bili_execute":
            return {"verified": True}
        raise AssertionError(name)

    client = SimpleNamespace(login=login, json_call=AsyncMock(side_effect=rpc))
    instance = SimpleNamespace(
        cfg=lambda: cfg,
        check_owner=lambda event: cfg,
        store=store,
        interactions=lambda: state,
        bili=lambda *args: client,
        _interaction_lock=asyncio.Lock(),
        require_goal=Mock(),
        is_reply_run=lambda event: False,
        active_goal=lambda event: None,
        reserve_share=Mock(),
        contact_send=AsyncMock(return_value={"message_id": "qq-receipt"}),
        sender=lambda: SimpleNamespace(send=AsyncMock()),
    )
    for method in methods:
        setattr(instance, method.name, MethodType(namespace[method.name], instance))
    event = SimpleNamespace(
        unified_msg_origin=cfg["owner_scope"],
        get_extra=lambda key: None,
        message_obj=SimpleNamespace(message_id="message-1"),
    )
    yield instance, event, cfg, state, client, calls
    store.close()


@pytest.mark.asyncio
async def test_stale_invalid_rechecks_then_auto_executes_without_asking(controller):
    instance, event, cfg, state, client, calls = controller
    instance.store.setting("interaction_login:" + event.unified_msg_origin, "invalid")
    result = json.loads(
        await instance.propose(event, "follow", '{"uid":22697887}', "want to follow")
    )
    assert [name for name, _ in calls] == ["bili_read", "bili_preview", "bili_execute"]
    assert result["authorization"] == "owner_configuration"
    assert result["verified"] is True
    assert (
        instance.store.get_setting("interaction_login:" + event.unified_msg_origin)
        == "valid"
    )
    instance.contact_send.assert_not_awaited()
    instance.reserve_share.assert_not_called()
    assert state.status(event.unified_msg_origin, cfg)["total"]["count"] == 1
    await instance.propose(event, "follow", '{"uid":22697887}', "same target")
    assert len([name for name, _ in calls if name == "bili_execute"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "login",
    [
        {"logged_in": False},
        {"logged_in": None},
        {},
        None,
        [],
        {"logged_in": True, "ok": False},
        {"logged_in": True, "risk_code": -412},
        {"logged_in": False, "risk_code": -101},
        RuntimeError("transport unavailable"),
    ],
)
async def test_unknown_invalid_or_risk_login_never_unlocks_a_write(controller, login):
    instance, event, cfg, state, client, calls = controller
    instance.store.setting("interaction_login:" + event.unified_msg_origin, "invalid")
    client.login = login
    result = json.loads(
        await instance.propose(event, "follow", '{"uid":22697887}', "test")
    )
    assert result["proposal_created"] is False
    assert [name for name, _ in calls] == ["bili_read"]
    assert (
        instance.store.get_setting("interaction_login:" + event.unified_msg_origin)
        == "invalid"
    )
    assert state.status(event.unified_msg_origin, cfg)["total"]["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["cooldown", "stopped", "uncertain", "quota"])
async def test_recovered_login_does_not_clear_other_protections(controller, guard):
    instance, event, cfg, state, client, calls = controller
    scope = event.unified_msg_origin
    args = {"uid": 22697887, "account_uid": 10}
    if guard == "cooldown":
        instance.store.setting("interaction_cooldown:" + scope, time.time() + 300)
    elif guard == "stopped":
        instance.store.setting("interaction_stopped:" + scope + ":qq:message-1", True)
    elif guard == "uncertain":
        state.reserve(scope, "old-run", "old-id", "follow", args, cfg)
    else:
        cfg["max_actions_per_day"] = 0
    instance.store.setting("interaction_login:" + scope, "invalid")
    result = json.loads(
        await instance.propose(event, "follow", '{"uid":22697887}', "test")
    )
    assert result["proposal_created"] is False
    assert "bili_execute" not in [name for name, _ in calls]
    if guard in {"cooldown", "stopped"}:
        assert not calls
        assert instance.store.get_setting("interaction_login:" + scope) == "invalid"
    else:
        assert instance.store.get_setting("interaction_login:" + scope) == "valid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        '{"mid":22697887}',
        '{"uid":22697887,"mid":22697887}',
        "{}",
        '{"uid":true}',
        '{"uid":0}',
        '{"uid":-1}',
        '{"uid":1.5}',
        '{"uid":"+5"}',
        '{"uid":"005"}',
        '{"uid":"五"}',
    ],
)
async def test_follow_contract_rejects_bad_fields_before_network(controller, arguments):
    instance, event, cfg, state, client, calls = controller
    result = json.loads(await instance.propose(event, "follow", arguments, "test"))
    assert result["proposal_created"] is False
    assert '"uid":22697887' in result["reason"]
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"interaction_mode": "approval"},
        {"auto_collection_management": False},
        {"allowed_uids": [7]},
        {"allowed_keywords": ["different title"]},
    ],
)
async def test_unmatched_auto_policy_uses_real_qq_approval(controller, changes):
    instance, event, cfg, state, client, calls = controller
    cfg.update(changes)
    result = json.loads(
        await instance.propose(event, "follow", '{"uid":"22697887"}', "test")
    )
    assert result["status"] == "awaiting_user"
    assert [name for name, _ in calls] == ["bili_preview"]
    instance.contact_send.assert_awaited_once()
    row = instance.store.db.execute("SELECT * FROM proposals").fetchone()
    assert row["status"] == "offered"
    assert json.loads(row["offer_receipt"])["message_id"] == "qq-receipt"


@pytest.mark.asyncio
async def test_approval_execute_also_rechecks_stale_login(controller):
    instance, event, cfg, state, client, calls = controller
    scope = event.unified_msg_origin
    proposal, _ = instance.store.propose(
        scope, cfg["owner_id"], "follow", {"uid": 5, "account_uid": 10}
    )
    instance.store.offered(proposal["id"], {"message_id": "qq-offer"})
    approved = instance.store.decide(scope, cfg["owner_id"], "批准", "qq-approval")
    instance.store.setting("interaction_login:" + scope, "invalid")
    result = await instance.execute_interaction(event, approved)
    assert result["verified"] is True
    assert [name for name, _ in calls] == ["bili_read", "bili_execute"]


@pytest.mark.asyncio
async def test_status_reuses_login_refresh_and_reports_unknown_as_unchecked(controller):
    instance, event, cfg, state, client, calls = controller
    instance.store.setting("interaction_login:" + event.unified_msg_origin, "invalid")
    client.login = {"logged_in": None, "risk_code": -412}
    result = json.loads(await instance.interaction_status(event))
    assert result["login"] == "invalid"
    assert result["login_checked"] is False
    assert result["cooldown_remaining_seconds"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cooling", [False, True])
async def test_reply_poll_rechecks_old_login_but_never_contacts_during_cooldown(
    controller, cooling
):
    instance, event, cfg, state, client, calls = controller
    cfg.update(
        enabled=True,
        reply_watch_enabled=True,
        public_interactions=True,
        max_minutes=1,
        max_model_calls_per_run=4,
    )
    scope = event.unified_msg_origin
    instance.store.setting("interaction_login:" + scope, "invalid")
    if cooling:
        instance.store.setting("interaction_cooldown:" + scope, time.time() + 300)
    watcher = SimpleNamespace(threads=lambda *args: [], candidate=lambda *args: None)
    instance.reply_watch = lambda: watcher
    instance.make_event = AsyncMock(return_value=event)
    budget = SimpleNamespace(start_run=lambda *args: 5, finish_run=Mock())
    instance.activity_budget = lambda: budget
    instance.agent = AsyncMock()

    def owner(current, **kwargs):
        if kwargs.get("interaction_guard"):
            state.guard(scope, instance.interaction_run(current))
        return cfg

    instance.check_owner = owner
    await instance.poll_replies()
    assert [name for name, _ in calls] == ([] if cooling else ["bili_read"])
    assert instance.store.get_setting("interaction_login:" + scope) == (
        "invalid" if cooling else "valid"
    )
    budget.finish_run.assert_called_once()
    assert not instance.reply_runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,arguments",
    [
        ("like", {"bvid": "BV-test", "cid": 1, "like": 1}),
        ("coin", {"bvid": "BV-test", "cid": 1, "multiply": 1}),
        ("favorite_add", {"bvid": "BV-test", "cid": 1, "folder_id": 7}),
        ("follow", {"uid": 22697887}),
    ],
)
async def test_enabled_engagement_and_collection_actions_execute_without_qq_approval(
    controller, action, arguments
):
    instance, event, cfg, state, client, calls = controller
    result = json.loads(
        await instance.propose(event, action, json.dumps(arguments), "useful content")
    )
    assert result["authorization"] == "owner_configuration"
    assert result["verified"] is True
    assert [name for name, _ in calls] == ["bili_preview", "bili_execute"]
    instance.contact_send.assert_not_awaited()
    assert state.status(event.unified_msg_origin, cfg)["today"][action]["count"] == 1


def test_prompt_matches_switches_and_quotas_without_granting_target_permission():
    cfg = DEFAULTS | {
        "engagement_actions": True,
        "collection_management": True,
        "auto_collection_management": True,
        "interaction_mode": "auto_low_risk",
    }
    prompt = authorization_prompt(cfg)
    actions = prompt.split("可按配置自动执行的动作=", 1)[1].split("；", 1)[0].split(",")
    assert {"like", "coin", "follow", "unfollow"} <= set(actions)
    assert not {"comment", "reply", "danmaku"} & set(actions)
    assert "目标允许范围" in prompt
    assert "不要在提交前先问批准" in prompt
    assert "awaiting_user/offered" in prompt
    assert "动作=无；" in authorization_prompt(cfg | {"interaction_mode": "approval"})
    assert "coin" not in authorization_prompt(cfg | {"max_coins_per_day": 0}).split(
        "动作=", 1
    )[1].split("；", 1)[0].split(",")
