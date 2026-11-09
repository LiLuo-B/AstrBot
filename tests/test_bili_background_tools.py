"""Verify background tool preparation without real model, Bilibili, or QQ I/O."""

import ast
import json
import sys
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from data.plugins.astrbot_plugin_bili_activity.access import group_access
from data.plugins.astrbot_plugin_bili_activity.core import quiet_now
from data.plugins.astrbot_plugin_bili_activity.interactions import authorization_prompt

SOURCE = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_bili_activity/main.py"
SCOPE = "qq:FriendMessage:123"
LIFE = {"df_goal", "df_recall", "df_interest", "df_life_status"}
STUDY = LIFE | {"df_workshop", "df_method"}


class Tools:
    """Minimal mutable tool collection matching the request interface."""

    def __init__(self, tools=None):
        self.tools = list(tools or [])

    def names(self):
        return [tool.name for tool in self.tools]

    def remove_tool(self, name):
        self.tools[:] = [tool for tool in self.tools if tool.name != name]


@pytest.fixture
def setup():
    """Bind real request filtering and owner checks to isolated runtime state."""
    cls = next(
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "BiliActivity"
    )
    selected = [
        n
        for n in cls.body
        if getattr(n, "name", None)
        in {
            "guard_phone",
            "check_owner",
            "is_life_run",
            "is_reply_run",
            "agent",
            "life_status",
        }
    ]
    for method in selected:
        method.decorator_list = []
    ns = {
        "AstrMessageEvent": object,
        "ProviderRequest": lambda **kw: SimpleNamespace(system_prompt="", **kw),
        "ToolSet": Tools,
        "json": json,
        "model_json": lambda value: json.dumps(value, ensure_ascii=False),
        "SelfExpression": lambda store: SimpleNamespace(context=lambda *args: ""),
        "quiet_now": quiet_now,
        "group_access": group_access,
        "authorization_prompt": authorization_prompt,
        "trim_tool_image_history": lambda *args, **kw: 0,
        "LIFE_TOOLS": LIFE,
        "WORKSHOP_TOOLS": {"df_workshop"},
        "METHOD_TOOLS": {"df_method"},
        "PROJECT_TOOL": "df_project",
        "EXPRESSION_TOOL": "df_self_reflect",
        "projects_enabled": lambda cfg: False,
        "expression_enabled": lambda cfg: False,
        "Projects": lambda store: SimpleNamespace(goal_gate=lambda *args: None),
        "normalized_server_name": lambda value: value,
        "PHONE_SERVERS": set(),
        "UNLABELLED_SERVERS": set(),
        "PHONE_NAMES": set(),
        "BILI_RAW_NAMES": {"bili_execute"},
        "BILI_SERVERS": set(),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), ns)
    cfg = {
        "owner_scope": SCOPE,
        "owner_id": "123",
        "group_history_enabled": True,
        "enabled": True,
        "life_enabled": True,
        "life_decision_enabled": True,
        "workshop_enabled": True,
        "methods_enabled": True,
        "dnd_ranges": [],
        "comment_learning_enabled": False,
        "reply_watch_enabled": True,
        "public_interactions": False,
    }
    settings = {}
    controller = SimpleNamespace(
        cfg=lambda: cfg,
        shared_scope=lambda event: SCOPE,
        store=SimpleNamespace(
            get_setting=lambda key, default=None: settings.get(key, default),
            life=SimpleNamespace(
                goals=lambda *args, **kw: [], interests=lambda *args, **kw: []
            ),
        ),
        workshop_store=lambda: SimpleNamespace(list=lambda *args, **kw: []),
        methods_store=lambda: SimpleNamespace(list=lambda *args, **kw: []),
        run_videos={"background": set()},
        life_runs={"background"},
        reply_runs={},
        methods_enabled=lambda cfg: cfg["life_enabled"] and cfg["methods_enabled"],
        active_goal=lambda event: None,
    )
    for method in selected:
        setattr(controller, method.name, MethodType(ns[method.name], controller))

    def event(scope=SCOPE, sender="123", run="background"):
        return SimpleNamespace(
            unified_msg_origin=scope,
            get_sender_id=lambda: sender,
            get_extra=lambda key: run if key == "df_run_id" else None,
        )

    def request():
        return SimpleNamespace(
            contexts=[],
            system_prompt="persona",
            func_tool=Tools(
                [
                    SimpleNamespace(name=name, active=True)
                    for name in sorted(
                        STUDY | {"df_bili_read", "df_bili_propose", "bili_execute"}
                    )
                ]
            ),
        )

    return controller, cfg, settings, event, request, ns


@pytest.mark.asyncio
@pytest.mark.parametrize("run", ["background", "writing", "review", "legacy-browse"])
async def test_controller_background_keeps_all_enabled_study_tools(setup, run):
    controller, cfg, settings, event, request, ns = setup
    controller.run_videos[run] = set()
    if run != "legacy-browse":
        controller.life_runs.add(run)
    req = request()
    await controller.guard_phone(event(run=run), req)
    assert STUDY <= set(req.func_tool.names())
    assert "bili_execute" not in req.func_tool.names()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown-run",
        "other-dm",
        "other-adapter",
        "wrong-owner",
        "group-disabled",
        "disabled",
        "paused",
        "dnd",
    ],
)
async def test_invalid_or_stopped_background_does_not_gain_study_tools(setup, case):
    controller, cfg, settings, event, request, ns = setup
    current = event()
    if case == "unknown-run":
        current = event(run="forged-run")
    elif case == "other-dm":
        current = event(scope="qq:FriendMessage:456")
    elif case == "other-adapter":
        current = event(scope="other:FriendMessage:123")
    elif case == "wrong-owner":
        current = event(sender="456", run=None)
    elif case == "group-disabled":
        cfg["group_history_enabled"] = False
        current = event(scope="qq:GroupMessage:456", run=None)
    elif case == "disabled":
        cfg["enabled"] = False
    elif case == "paused":
        settings["life_paused:" + SCOPE] = True
    else:
        cfg["dnd_ranges"] = [["00:00", "23:59"], ["23:59", "00:00"]]
    req = request()
    await controller.guard_phone(current, req)
    assert not STUDY & set(req.func_tool.names())
    with pytest.raises(PermissionError):
        await controller.life_status(current)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,sender,expected",
    [
        (SCOPE, "123", STUDY - {"df_recall"}),
        (SCOPE, "456", set()),
        ("qq:FriendMessage:456", "123", set()),
        ("qq:GroupMessage:456", "another", STUDY),
        ("other:GroupMessage:456", "123", set()),
    ],
)
async def test_regular_chat_scope_and_recall_visibility(setup, scope, sender, expected):
    controller, cfg, settings, event, request, ns = setup
    req = request()
    await controller.guard_phone(event(scope=scope, sender=sender, run=None), req)
    assert set(req.func_tool.names()) & STUDY == expected
    if ":GroupMessage:" in scope and scope.startswith("qq:"):
        assert "df_bili_propose" not in req.func_tool.names()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "switch,removed",
    [
        ("life_enabled", STUDY),
        ("workshop_enabled", {"df_workshop"}),
        ("methods_enabled", {"df_method"}),
    ],
)
async def test_feature_switches_still_remove_disabled_tools(setup, switch, removed):
    controller, cfg, settings, event, request, ns = setup
    cfg[switch] = False
    req = request()
    await controller.guard_phone(event(), req)
    assert not removed & set(req.func_tool.names())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wanted",
    [
        STUDY,
        LIFE | {"df_workshop"},
        {"df_workshop", "df_recall", "df_life_status"},
        STUDY | {"unregistered_tool"},
    ],
)
async def test_real_agent_preflight_survives_filter_for_background_modes(setup, wanted):
    controller, cfg, settings, event, request, ns = setup
    current = event()
    current.is_stopped = lambda: False
    current.plugins_name = None
    controller.context = SimpleNamespace(
        get_config=lambda **kw: {"provider_settings": {}}
    )
    controller.is_life_run = lambda event: False
    runner = SimpleNamespace(
        state="done",
        get_final_llm_resp=lambda: SimpleNamespace(completion_text="offline completed"),
    )
    started = []

    async def steps(max_steps):
        started.append(max_steps)
        if False:
            yield None

    runner.step_until_done = steps

    async def reset():
        return None

    async def build(**kw):
        kw["req"].func_tool = request().func_tool
        return SimpleNamespace(agent_runner=runner, reset_coro=reset())

    async def hook(event, kind, *args):
        if kind == "request":
            await controller.guard_phone(event, args[0])

    specs = {
        "astrbot.core.astr_main_agent": {
            "MainAgentBuildConfig": lambda **kw: kw,
            "_get_session_conv": AsyncMock(return_value=SimpleNamespace(history="[]")),
            "build_main_agent": build,
        },
        "astrbot.core.pipeline.context_utils": {"call_event_hook": hook},
        "astrbot.core.star.star_handler": {
            "EventType": SimpleNamespace(
                OnWaitingLLMRequestEvent="waiting", OnLLMRequestEvent="request"
            )
        },
        "astrbot.core.agent.runners.base": {
            "AgentState": SimpleNamespace(ERROR="error")
        },
    }
    modules = {}
    for name, values in specs.items():
        module = ModuleType(name)
        vars(module).update(values)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        if "unregistered_tool" in wanted:
            with pytest.raises(
                RuntimeError, match="活动工具尚未注册: unregistered_tool"
            ):
                await controller.agent(current, "offline test", wanted, max_steps=1)
            assert not started
            return
        result = await controller.agent(current, "offline test", wanted, max_steps=1)
    assert result == "offline completed"
    assert started == [1]
