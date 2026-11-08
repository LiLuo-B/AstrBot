import ast
import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_bili_activity.access import group_access, public_history

ROOT = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_bili_activity/main.py"


def event(scope="千秋理子（三方）:GroupMessage:123", sender="474115040"):
    class Event:
        unified_msg_origin = scope
        def get_sender_id(self):
            return sender
        def get_extra(self, key):
            return None
    return Event()


def config(**kw):
    return {"owner_scope": "千秋理子（三方）:FriendMessage:474115040",
            "owner_id": "474115040", "group_history_enabled": True,
            "group_owner_actions_enabled": True, **kw}


def methods(name):
    tree = ast.parse(ROOT.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BiliActivity")
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}[name]


def test_group_read_is_opt_in_and_read_only():
    assert group_access(event(), config())
    assert not group_access(event(), config(group_history_enabled=False))
    assert not group_access(event(), config(group_owner_actions_enabled=False), owner=True)
    assert not group_access(event(sender="not-owner"), config(), owner=True)
    assert not group_access(event(scope="other:GroupMessage:123"), config())
    assert not group_access(event(scope="千秋理子（三方）:GroupMessage:123:extra"), config())
    assert not group_access(event(scope="千秋理子（三方）:FriendMessage:474115040"), config())


def test_group_result_drops_private_fields():
    result = public_history({"counts": {"browsed_videos": 1}, "range": {"start": "2026-11-08"},
        "coverage": {"records_are_not_complete": True}, "limitations": ["private? no"],
        "videos": [{"bvid": "BV1", "title": "公开标题", "notes": ["主人的私密感想"],
                    "evidence_ids": ["private-episode"], "media": ["subtitles"]}]})
    assert "notes" not in result["videos"][0]
    assert "evidence_ids" not in result["videos"][0]
    assert "private-episode" not in json.dumps(result, ensure_ascii=False)


def test_controller_uses_group_gate_and_strips_private_history():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("bili_history"))
    assert "check_chat(event)" in source
    assert "public_history(result)" in source
    assert "cfg[\"owner_scope\"] if group" in source


def test_group_write_requires_owner_and_uses_private_ledger():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("propose"))
    assert "check_chat(event, owner=True)" in source
    assert "scope = cfg[\"owner_scope\"] if group" in source
    assert "await event.send(event.plain_result(text))" in source
    assert "self.require_goal(event)" in source
    # The foreground group command is not a background life run and must not demand a private goal.
    assert "if not group:\n            self.require_goal(event)" in source


def test_group_prompt_removes_private_tools_before_other_hooks():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("guard_phone"))
    assert "allowed = {\"df_bili_history\"}" in source
    assert "allowed.add(\"df_bili_propose\")" in source
    assert "return" in source


def test_execution_and_group_approval_use_same_access_and_ledger():
    source = ROOT.read_text(encoding="utf-8")
    execution = ast.get_source_segment(source, methods("execute_interaction"))
    automatic = ast.get_source_segment(source, methods("automatic_proposal"))
    incoming = ast.get_source_segment(source, methods("incoming"))
    assert execution.count("check_chat(event, owner=True)") == 2
    assert "check_chat(event, owner=True)" in automatic
    assert 'proposal["id"], proposal["scope"]' in automatic
    assert "store.decide(ledger_scope" in incoming
    assert "store.queue_memory(ledger_scope" in incoming
    assert "if not group_owner:\n                await self.flush_memory(event)" in incoming


def test_invalid_group_switch_is_rejected():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("cfg"))
    assert '"group_history_enabled", "group_owner_actions_enabled"' in source
