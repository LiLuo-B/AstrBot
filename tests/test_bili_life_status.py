"""Check shareable life summaries, fixed activity queries, and SELECT-only reads."""

import ast
import json
from datetime import datetime
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import docstring_parser
import pytest

from data.plugins.astrbot_plugin_bili_activity.access import group_access
from data.plugins.astrbot_plugin_bili_activity.core import (
    Store,
    model_json,
    quiet_now,
    time_context,
)
from data.plugins.astrbot_plugin_bili_activity.planner import LifePlanner
from data.plugins.astrbot_plugin_bili_activity.projects import Projects
from data.plugins.astrbot_plugin_bili_activity.self_expression import SelfExpression

SOURCE = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_bili_activity/main.py"
SCOPE = "qq:FriendMessage:123"
NOW = datetime.fromisoformat("2026-11-09T15:00:00+08:00").timestamp()


def test_tool_registration_exposes_no_model_supplied_event_parameter():
    """AstrBot derives tool parameters from the Google-style docstring."""
    cls = next(
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "BiliActivity"
    )
    method = next(n for n in cls.body if getattr(n, "name", None) == "life_status")
    assert docstring_parser.parse(ast.get_docstring(method)).params == []


@pytest.fixture
def state(tmp_path):
    """Use real temporary ledgers and real read-only summary dependencies."""
    store = Store(tmp_path)
    cfg = {
        "owner_scope": SCOPE,
        "owner_id": "123",
        "group_history_enabled": True,
        "life_enabled": True,
        "life_decision_enabled": True,
        "enabled": True,
        "workshop_enabled": True,
        "methods_enabled": True,
        "self_expression_enabled": True,
        "dnd_ranges": [],
        "reply_watch_enabled": False,
        "public_interactions": False,
    }
    goal = store.life.create_goal(
        SCOPE,
        "Research topic",
        "Compare actual sources",
        "Read another source",
        "goal-source",
    )
    store.db.execute("UPDATE goals SET updated_at=? WHERE id=?", (NOW - 60, goal["id"]))
    old_goal = store.life.create_goal(
        SCOPE, "Old unfinished topic", "Old intent", "Still unresolved", "old-source"
    )
    store.db.execute(
        "UPDATE goals SET updated_at=? WHERE id=?", (NOW - 90 * 86400, old_goal["id"])
    )
    store.life.create_goal(
        "qq:GroupMessage:456",
        "Wrong group record",
        "Must not be queried",
        "Wrong",
        "other-source",
    )
    store.event(
        SCOPE,
        "life_decision",
        {
            "run_id": "run",
            "at": NOW - 120,
            "action": "research",
            "reason": "Continue comparing sources",
            "topic": "topic",
            "score": 99,
            "factors": [{"secret": "INTERNAL_MARKER"}],
            "excluded": ["INTERNAL_MARKER"],
            "contacts": "PRIVATE_MARKER",
        },
    )
    store.db.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?)",
        ("run", SCOPE, NOW - 120, NOW - 30, "timeout", "PRIVATE_MARKER"),
    )
    store.setting("life_paused:" + SCOPE, True)
    projects = Projects(store)
    projects.ready(create=True)
    store.db.execute(
        "INSERT INTO life_projects VALUES(?,?,?,?,?,?,?,?)",
        ("project", SCOPE, "Research project", "why", "active", 3, NOW - 300, NOW - 20),
    )
    store.db.execute(
        "INSERT INTO project_stages VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "project",
            SCOPE,
            1,
            goal["id"],
            "stage-source",
            "Stage",
            "why",
            "next",
            NOW - 300,
        ),
    )
    cls = next(
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "BiliActivity"
    )
    methods = [
        n
        for n in cls.body
        if getattr(n, "name", None)
        in {"life_status", "check_owner", "is_life_run", "is_reply_run"}
    ]
    for n in methods:
        n.decorator_list = []
    ns = {
        "AstrMessageEvent": object,
        "time": SimpleNamespace(time=lambda: NOW),
        "SelfExpression": SelfExpression,
        "Projects": Projects,
        "quiet_now": quiet_now,
        "group_access": group_access,
        "model_json": model_json,
        "time_context": time_context,
        "projects_enabled": lambda cfg: True,
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), ns)
    controller = SimpleNamespace(
        cfg=lambda: cfg,
        store=store,
        run_videos={"background": set()},
        life_runs={"background"},
        reply_runs={},
        active_goal=lambda event: None,
        life_planner=lambda: LifePlanner(store),
        methods_enabled=lambda cfg: cfg["life_enabled"] and cfg["methods_enabled"],
        contact_ledger=Mock(
            side_effect=AssertionError("contact ledger must not be read")
        ),
        methods_store=Mock(
            side_effect=AssertionError("fresh method checks must not run")
        ),
    )
    for n in methods:
        setattr(controller, n.name, MethodType(ns[n.name], controller))

    def event(scope=SCOPE, sender="123", run=None):
        return SimpleNamespace(
            unified_msg_origin=scope,
            get_sender_id=lambda: sender,
            get_extra=lambda key: run if key == "df_run_id" else None,
        )

    yield controller, store, cfg, event, ns
    store.close()


@pytest.mark.asyncio
async def test_group_and_owner_receive_same_whitelisted_activity_view(state):
    controller, store, cfg, event, ns = state
    statements = []
    store.db.execute("PRAGMA query_only=ON")
    store.db.set_trace_callback(statements.append)
    before = store.db.total_changes
    owner = json.loads(await controller.life_status(event()))
    group = json.loads(
        await controller.life_status(event(scope="qq:GroupMessage:456", sender="other"))
    )
    assert owner == group
    assert set(owner) == {
        "as_of",
        "window_days",
        "activity",
        "focus",
        "interests",
        "completed_goals",
        "interruptions",
        "decisions",
        "projects",
        "coverage",
        "meaning",
        "time",
    }
    assert owner["activity"]["paused"] is True
    assert owner["focus"][0]["title"] == "Research topic"
    assert all(item["intent_only"] is True for item in owner["focus"])
    assert owner["decisions"][0]["intent_only"] is True
    assert owner["decisions"][0]["run_status"] == "timeout"
    assert owner["interruptions"][0]["status"] == "timeout"
    assert owner["projects"][0]["planned_stage_count"] == 1
    for timestamp in (
        owner["decisions"][0]["occurred_at"],
        owner["interruptions"][0]["occurred_at"],
        owner["projects"][0]["updated_at"],
    ):
        assert isinstance(timestamp, str) and timestamp.startswith("2026-11-09")
    raw = json.dumps(owner)
    for forbidden in (
        "PRIVATE_MARKER",
        "INTERNAL_MARKER",
        "Wrong group record",
        "contacts",
        "last_owner_message",
        "message_id",
        "memory_policy",
        "score",
        "factors",
        "excluded",
        "scan_limit",
        "max_seconds",
        "goal_id",
        "run_id",
    ):
        assert forbidden not in raw
    assert store.db.total_changes == before
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    controller.contact_ledger.assert_not_called()


@pytest.mark.asyncio
async def test_status_independent_of_optional_reflection_flag(state):
    controller, store, cfg, event, ns = state
    cfg["self_expression_enabled"] = False
    result = json.loads(await controller.life_status(event()))
    assert result["focus"]
    assert cfg["self_expression_enabled"] is False


@pytest.mark.asyncio
async def test_partial_project_schema_preserves_nearby_activity_and_reports_limit(
    state,
):
    controller, store, cfg, event, ns = state
    store.db.execute("DROP TABLE project_reviews")
    store.db.execute("PRAGMA query_only=ON")
    result = json.loads(
        await controller.life_status(event(scope="qq:GroupMessage:456"))
    )
    assert result["projects"] == []
    assert result["focus"]
    assert result["coverage"]["partial"] is True
    assert any(
        "项目记录暂时不可读" in note for note in result["coverage"]["limitations"]
    )


@pytest.mark.asyncio
async def test_status_projection_preserves_completion_uncertainty_and_interest_limits(
    state,
):
    controller, store, cfg, event, ns = state
    snapshot = {
        "as_of": "2026-11-09 15:00",
        "window_days": 14,
        "focus": [],
        "interests": [
            {
                "topic": "topic",
                "state": "tentative",
                "recent_direction": None,
                "previous_direction": None,
                "recent_sources": [{"private": "PRIVATE_MARKER"}],
                "recent_origin_count": 1,
                "meaning": "one observation is not a lasting preference",
            }
        ],
        "completed_goals": [
            {
                "title": "Claimed completion",
                "basis": "needs_recheck",
                "recorded_at": NOW - 60,
                "source_ids": [],
            }
        ],
        "interruptions": [],
        "coverage": {"episode_history_truncated": True},
    }
    ns["SelfExpression"] = lambda store: SimpleNamespace(
        snapshot=lambda *args, **kw: snapshot
    )
    result = json.loads(await controller.life_status(event()))
    assert result["completed_goals"][0]["basis"] == "needs_recheck"
    assert result["interests"][0]["state"] == "tentative"
    assert result["coverage"]["partial"] is True
    assert "PRIVATE_MARKER" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_status,attempt_status",
    [("active", "active"), ("retired", "active"), ("active", "paused")],
)
async def test_method_background_status_never_refreshes_validation_or_creates_schema(
    state, method_status, attempt_status
):
    controller, store, cfg, event, ns = state
    store.setting("life_paused:" + SCOPE, False)
    store.db.executescript("""
        CREATE TABLE research_methods(id TEXT,scope TEXT,status TEXT,revision INTEGER);
        CREATE TABLE method_attempts(method_id TEXT,revision INTEGER,scope TEXT,goal_id TEXT,run_id TEXT,status TEXT);
    """)
    store.db.execute(
        "INSERT INTO research_methods VALUES(?,?,?,?)",
        ("method", SCOPE, method_status, 1),
    )
    store.db.execute(
        "INSERT INTO method_attempts VALUES(?,?,?,?,?,?)",
        ("method", 1, SCOPE, "goal", "background", attempt_status),
    )
    controller.method_runs = {"background"}
    controller.active_goal = lambda event: {"id": "goal"}
    store.db.execute("PRAGMA query_only=ON")
    before = store.db.total_changes
    if method_status == "retired" or attempt_status != "active":
        with pytest.raises(PermissionError):
            await controller.life_status(event(run="background"))
    else:
        result = json.loads(await controller.life_status(event(run="background")))
        assert result["focus"]
    assert store.db.total_changes == before
    controller.methods_store.assert_not_called()
