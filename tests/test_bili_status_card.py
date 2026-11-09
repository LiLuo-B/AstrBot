"""Bounded activity context with dated evidence and unchanged query permissions."""

import ast
import importlib
import json
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "data/plugins/astrbot_plugin_bili_activity"
PACKAGE = "bili_status_card_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules[PACKAGE] = package
expression = importlib.import_module(f"{PACKAGE}.self_expression")
life = importlib.import_module(f"{PACKAGE}.life")
NOW = datetime.fromisoformat("2026-11-09T09:00:00+08:00").timestamp()
SCOPE = "qq:FriendMessage:123"
CFG = {"life_enabled": True, "self_expression_enabled": True}


@pytest.fixture
def state():
    """Create an isolated ledger without initializing production state."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE goals(id TEXT,scope TEXT,owner_kind TEXT,status TEXT,title TEXT,updated_at REAL)"
    )
    db.execute(
        "CREATE TABLE episodes(id TEXT,scope TEXT,kind TEXT,subject_kind TEXT,occurred_at REAL,evidence_json TEXT,interpretation_json TEXT,supersedes_id TEXT)"
    )
    store = SimpleNamespace(db=db)
    store.life = SimpleNamespace(
        goals=lambda scope: [
            dict(row)
            for row in db.execute(
                "SELECT * FROM goals WHERE scope=? AND status NOT IN ('completed','cancelled') ORDER BY updated_at DESC",
                (scope,),
            )
        ]
    )
    yield db, store
    db.close()


def add_episode(
    db,
    ident="latest",
    *,
    kind="video_note",
    proof=None,
    thought="当时觉得评分不稳定",
    at=NOW - 86400,
    scope=SCOPE,
    subject="agent",
    supersedes=None,
):
    """Insert one evidence fixture and keep its event time explicit.

    Args:
        db: In-memory ledger.
        ident: Unique episode identifier.
        kind: Recorded activity type.
        proof: Structured tool evidence.
        thought: Attributed note, not a factual conclusion.
        at: Source event timestamp.
        scope: Activity identity.
        subject: Evidence owner category.
        supersedes: Prior episode corrected by this entry.
    """
    if proof is None:
        proof = {
            "bvid": "BVexample",
            "cid": 1,
            "subtitles": [{"content": "subtitle"}],
            "observations": [],
        }
    db.execute(
        "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)",
        (
            ident,
            scope,
            kind,
            subject,
            at,
            json.dumps(proof),
            json.dumps({"thought": thought}),
            supersedes,
        ),
    )


def test_card_is_dated_bounded_and_read_only(state):
    """Prefer source evidence over a newer run summary or recommendation list."""
    db, store = state
    db.execute(
        "INSERT INTO goals VALUES(?,?,?,?,?,?)",
        ("goal", SCOPE, "agent", "paused", "验证评分稳定性" * 50, NOW - 90000),
    )
    add_episode(
        db,
        proof={
            "bvid": "BVexample",
            "cid": 1,
            "subtitles": [{"content": "字幕" * 1000}],
        },
        thought="观点" * 1000,
    )
    add_episode(
        db,
        "info",
        kind="read",
        at=NOW - 86500,
        proof={
            "operation": "info",
            "result_excerpt": {
                "identity": {"bvid": "BVexample", "cid": 1, "title": "AI设计视频" * 100}
            },
        },
    )
    add_episode(
        db,
        "summary",
        kind="run_end",
        at=NOW - 10,
        proof={"status": "completed"},
        thought="今天完整看完了很多视频",
    )
    add_episode(
        db,
        "feed",
        kind="read",
        at=NOW - 5,
        proof={
            "ok": True,
            "operation": "home",
            "result_excerpt": {
                "item": [{"bvid": "BVcandidate", "title": "不曾打开的推荐"}]
            },
        },
    )
    before = db.total_changes
    card = expression.SelfExpression(store).context(SCOPE, CFG, now=NOW)
    assert db.total_changes == before
    assert len(card) <= 500
    assert "截至2026-11-09 09:00" in card
    assert "实际记录（2026-11-08 09:00）" in card
    assert "意图（" in card and "非已完成" in card
    assert "取得：字幕" in card
    assert "AI设计视频" in card and "BVexample/latest" in card
    assert "自身观点、非事实或用户偏好" in card
    assert "不是完整历史或今天清单" in card
    assert "不曾打开的推荐" not in card and "今天完整看完" not in card
    assert "completed_goals" not in card


def test_card_stays_bounded_at_maximum_identity_and_text_lengths(state):
    """Preserve the source and limitations without relying on a final JSON cut."""
    db, store = state
    db.execute(
        "INSERT INTO goals VALUES(?,?,?,?,?,?)",
        ("goal", SCOPE, "agent", "ready", "G" * 1000, NOW - 10),
    )
    add_episode(
        db,
        "s" * 64,
        proof={
            "bvid": "BV" + "a" * 30,
            "cid": 1,
            "subtitles": [{"content": "text"}],
            "observations": [
                {"kind": "frames", "text": "frame"},
                {"kind": "asr", "text": "audio"},
            ],
        },
        thought="T" * 1000,
    )
    card = expression.SelfExpression(store).context(SCOPE, CFG, now=NOW)
    assert len(card) <= 500
    assert "字幕、抽样画面、语音转写" in card
    assert "s" * 64 in card
    assert card.endswith("查不到保持未知。")


@pytest.mark.parametrize(
    "proof,kind",
    [
        (
            {"ok": False, "bvid": "BVbad", "cid": 1, "subtitles": [{"content": "bad"}]},
            "video",
        ),
        ({"operation": [], "result_excerpt": {"bvid": "BVbad", "cid": 1}}, "read"),
        (
            {
                "operation": "search",
                "result_excerpt": {"bvid": "BVcandidate", "cid": 1},
            },
            "read",
        ),
        (
            {
                "operation": "info",
                "result_excerpt": {"ok": False, "bvid": "BVbad", "cid": 1},
            },
            "read",
        ),
        (
            {"bvid": "BVempty", "cid": 1, "subtitles": [], "observations": []},
            "video_note",
        ),
        (
            {
                "bvid": "BVempty",
                "cid": 1,
                "subtitles": [{"content": ""}],
                "observations": [{"kind": [], "text": "unrecognized"}],
            },
            "video",
        ),
    ],
)
def test_empty_failed_and_list_only_evidence_does_not_create_a_card(state, proof, kind):
    """Do not turn attempts, recommendations, or malformed media into progress."""
    db, store = state
    add_episode(db, kind=kind, proof=proof)
    assert expression.SelfExpression(store).context(SCOPE, CFG, now=NOW) == ""


def test_scope_freshness_subject_and_corrections_are_respected(state):
    """Old, future, user-owned, foreign, or superseded content is not current status."""
    db, store = state
    add_episode(db, "foreign", scope="other")
    add_episode(db, "user", subject="user")
    add_episode(db, "old", at=NOW - 15 * 86400)
    add_episode(db, "future", at=NOW + 10)
    add_episode(db, "corrected")
    add_episode(db, "correction", kind="run_end", supersedes="corrected")
    db.execute(
        "INSERT INTO goals VALUES(?,?,?,?,?,?)",
        ("finished", SCOPE, "agent", "completed", "Finished is not watching", NOW - 10),
    )
    assert expression.SelfExpression(store).context(SCOPE, CFG, now=NOW) == ""


def test_many_feed_reads_do_not_hide_the_latest_real_experience(state):
    """Filter recommendation reads before applying the bounded candidate scan."""
    db, store = state
    add_episode(db, at=NOW - 1000)
    for number in range(60):
        add_episode(
            db,
            f"feed{number}",
            kind="read",
            at=NOW - number,
            proof={
                "operation": "home",
                "result_excerpt": {"item": [{"bvid": "BVcandidate"}]},
            },
        )
    assert "BVexample/latest" in expression.SelfExpression(store).context(
        SCOPE, CFG, now=NOW
    )


def test_fact_only_read_does_not_claim_media_or_copy_model_thought(state):
    """Read metadata is not subtitles, frames, or the model's saved opinion."""
    db, store = state
    add_episode(
        db,
        kind="read",
        proof={
            "operation": "info",
            "result_excerpt": {"bvid": "BVinfo", "cid": 2, "title": "Only metadata"},
        },
        thought="I watched it all",
    )
    card = expression.SelfExpression(store).context(SCOPE, CFG, now=NOW)
    assert "Only metadata" in card
    assert "资料读取（未证明取得字幕或画面）" in card
    assert "当时感想" not in card
    assert "I watched it all" not in card
    assert len(card) <= 500


def test_disabled_card_does_not_query_storage():
    """The existing feature switch still prevents all status reads."""
    store = SimpleNamespace(db=Mock(), life=Mock())
    assert (
        expression.SelfExpression(store).context(
            SCOPE, {**CFG, "self_expression_enabled": False}, now=NOW
        )
        == ""
    )
    store.db.execute.assert_not_called()
    store.life.goals.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,sender,run_id,enabled,expected",
    [
        (SCOPE, "123", None, True, True),
        ("qq:GroupMessage:456", "another", None, True, True),
        ("qq:FriendMessage:other", "other", None, True, False),
        ("other:GroupMessage:456", "123", None, True, False),
        (SCOPE, "123", "background", True, False),
        (SCOPE, "123", None, False, False),
    ],
)
async def test_request_hook_only_adds_one_card_and_keeps_recall_visibility(
    scope, sender, run_id, enabled, expected
):
    """Share compact context without changing the existing tool access decision."""
    from data.plugins.astrbot_plugin_bili_activity.access import group_access
    from data.plugins.astrbot_plugin_bili_activity.interactions import (
        authorization_prompt,
    )

    tree = ast.parse((PLUGIN / "main.py").read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BiliActivity"
    )
    method = next(n for n in cls.body if getattr(n, "name", None) == "guard_phone")
    method.decorator_list = []
    card = Mock(return_value="\n[自身近况依据]（极简）测试资料")
    namespace = {
        "AstrMessageEvent": object,
        "trim_tool_image_history": lambda *args, **kwargs: 0,
        "group_access": group_access,
        "authorization_prompt": authorization_prompt,
        "expression_enabled": expression.enabled,
        "SelfExpression": Mock(return_value=SimpleNamespace(context=card)),
        "PROJECT_TOOL": "df_project",
        "EXPRESSION_TOOL": "df_self_reflect",
        "METHOD_TOOLS": {"df_method"},
        "WORKSHOP_TOOLS": {"df_workshop"},
        "LIFE_TOOLS": {"df_recall"},
        "normalized_server_name": lambda v: v,
        "PHONE_SERVERS": set(),
        "UNLABELLED_SERVERS": set(),
        "PHONE_NAMES": set(),
        "BILI_RAW_NAMES": set(),
        "BILI_SERVERS": set(),
    }
    exec(
        compile(
            ast.Module(body=[method], type_ignores=[]), str(PLUGIN / "main.py"), "exec"
        ),
        namespace,
    )
    cfg = {
        **CFG,
        "self_expression_enabled": enabled,
        "owner_scope": SCOPE,
        "owner_id": "123",
        "group_history_enabled": True,
        "comment_learning_enabled": False,
    }
    store = Mock()
    controller = SimpleNamespace(
        cfg=lambda: cfg, shared_scope=lambda event: SCOPE, store=store
    )
    event = SimpleNamespace(
        unified_msg_origin=scope,
        get_sender_id=lambda: sender,
        get_extra=lambda key: run_id if key == "df_run_id" else None,
    )
    tools = SimpleNamespace(tools=[SimpleNamespace(name="df_recall")])
    tools.remove_tool = lambda name: setattr(
        tools, "tools", [t for t in tools.tools if t.name != name]
    )
    request = SimpleNamespace(
        contexts=[], system_prompt="existing persona", func_tool=tools
    )
    await namespace["guard_phone"](controller, event, request)
    assert card.call_count == int(expected)
    assert request.system_prompt.count("[自身近况依据]") == int(expected)
    assert all(
        marker not in request.system_prompt
        for marker in ("持续目标与兴趣证据", "文字作品", "研究方法")
    )
    assert any(t.name == "df_recall" for t in tools.tools) == group_access(event, cfg)
    store.life.goals.assert_not_called()
    store.life.interests.assert_not_called()
