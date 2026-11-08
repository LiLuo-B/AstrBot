"""Shared chat activity scopes with account writes restricted to the owner DM."""

import ast
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_bili_activity.access import group_access

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
            "comment_learning_enabled": False, "life_enabled": False, **kw}


def methods(name):
    tree = ast.parse(ROOT.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BiliActivity")
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}[name]


def test_group_read_is_opt_in_and_adapter_scoped():
    assert group_access(event(), config())
    assert not group_access(event(), config(group_history_enabled=False))
    assert not group_access(event(scope="other:GroupMessage:123"), config())
    assert not group_access(event(scope="千秋理子（三方）:GroupMessage:123:extra"), config())
    assert not group_access(event(scope="千秋理子（三方）:FriendMessage:474115040"), config())


def test_history_reads_shared_owner_scope_without_redaction():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("bili_history"))
    assert "check_owner(event, shared=True)" in source
    assert 'browsing_history(self.store.db, cfg["owner_scope"]' in source
    assert "public_history" not in source


def test_write_execution_only_accepts_owner_private_chat():
    source = ROOT.read_text(encoding="utf-8")
    for name in ("execute_interaction", "automatic_proposal", "propose"):
        body = ast.get_source_segment(source, methods(name))
        assert "check_owner(event)" in body
        assert "shared=True" not in body
        assert "group_access" not in body


def test_group_tools_allow_reads_and_memory_but_not_account_writes():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("guard_phone"))
    assert 'tool.name != "df_bili_propose"' in source
    assert "群聊与私聊共享" in source


def test_shared_read_and_memory_tools_use_owner_activity_scope():
    source = ROOT.read_text(encoding="utf-8")
    for name in ("read", "watch", "goal", "method", "recall", "workshop"):
        body = ast.get_source_segment(source, methods(name))
        assert "check_owner(event, shared=True)" in body, name
        if name != "watch":
            assert "self.shared_scope(event)" in body, name
    assert "scope = self.shared_scope(event)" in ast.get_source_segment(source, methods("record_video"))


@pytest.mark.asyncio
@pytest.mark.parametrize("text,date", [
    ("[主人(474115040)] 今天刷了几条b站了", "今天"),
    ("昨天逛了什么视频", "昨天"),
    ("2026-11-07看了哪些B站视频", "2026-11-07"),
])
async def test_explicit_group_history_question_queries_before_answer(text, date):
    method = methods("incoming")
    method.decorator_list = []
    module = SimpleNamespace(AstrMessageEvent=object, group_access=lambda event, cfg: True,
        re=__import__("re"), json=json, logger=SimpleNamespace(warning=lambda *args: None))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT), "exec"), vars(module))
    controller = SimpleNamespace(cfg=lambda: config(), bili_history=AsyncMock(return_value=json.dumps({
        "start_date": "2026-11-08", "counts": {"browsed_videos": 2},
        "videos": [{"bvid": "BV1", "title": "第一条"}, {"bvid": "BV2", "title": "第二条"}],
        "next_offset": None}, ensure_ascii=False)))
    current = event(sender="other")
    current.message_str = text
    current.message_obj = SimpleNamespace(message_str=text, message_id="g1")
    current.send = AsyncMock()
    current.stop_event = Mock()
    current.plain_result = lambda value: value
    await module.incoming(controller, current)
    controller.bili_history.assert_awaited_once_with(current, date)
    current.stop_event.assert_called_once()
    answer = current.send.await_args.args[0]
    assert "2条不同视频" in answer and "《第一条》" in answer and "《第二条》" in answer
    assert "没刷" not in answer


@pytest.mark.asyncio
async def test_group_history_failure_does_not_deny_activity():
    method = methods("incoming")
    method.decorator_list = []
    module = SimpleNamespace(AstrMessageEvent=object, group_access=lambda event, cfg: True,
        re=__import__("re"), json=json, logger=SimpleNamespace(warning=lambda *args: None))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT), "exec"), vars(module))
    controller = SimpleNamespace(cfg=lambda: config(), bili_history=AsyncMock(side_effect=ValueError("bad")))
    current = event(sender="other")
    current.message_str = "今天刷了几条b站了"
    current.message_obj = SimpleNamespace(message_str=current.message_str, message_id="g1")
    current.send = AsyncMock()
    current.stop_event = Mock()
    current.plain_result = lambda value: value
    await module.incoming(controller, current)
    assert "暂时没查出来" in current.send.await_args.args[0]


@pytest.mark.asyncio
async def test_group_request_does_not_rewrite_qq_scope_and_removes_write_tool():
    module = ModuleType("bili_group_shared_request")
    module.AstrMessageEvent = object
    module.trim_tool_image_history = lambda *args, **kwargs: 0
    module.group_access = lambda event, cfg: True
    module.PROJECT_TOOL = "df_project"
    module.EXPRESSION_TOOL = "df_self_reflect"
    module.METHOD_TOOLS = {"df_method"}
    module.WORKSHOP_TOOLS = {"df_workshop"}
    module.LIFE_TOOLS = {"df_goal", "df_recall", "df_interest", "df_life_status"}
    module.normalized_server_name = lambda value: value
    module.PHONE_SERVERS = set()
    module.UNLABELLED_SERVERS = set()
    module.PHONE_NAMES = set()
    module.BILI_RAW_NAMES = set()
    module.BILI_SERVERS = set()
    method = methods("guard_phone")
    method.decorator_list = []
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT), "exec"), vars(module))
    controller = SimpleNamespace(cfg=lambda: config(), shared_scope=lambda event: config()["owner_scope"],
        is_reply_run=lambda event: False, store=SimpleNamespace(get_setting=lambda *args: False),
        reply_watch=lambda: SimpleNamespace(context=lambda *args: ""))
    current = event()
    current.message_str = "闲聊"
    write = SimpleNamespace(name="df_bili_propose")
    read = SimpleNamespace(name="df_bili_read")
    request = SimpleNamespace(contexts=[], system_prompt="persona", func_tool=SimpleNamespace(tools=[write, read]))
    await module.guard_phone(controller, current, request)
    assert current.unified_msg_origin.endswith(":GroupMessage:123")
    assert request.func_tool.tools == [read]
    assert "共享" in request.system_prompt


def test_only_group_history_switch_remains():
    source = ast.get_source_segment(ROOT.read_text(encoding="utf-8"), methods("cfg"))
    assert '"group_history_enabled"' in source
    assert "group_owner_actions_enabled" not in source
