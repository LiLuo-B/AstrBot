"""Keep factual sender metadata separate from unsupported social relationships."""

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
FACE_PATH = ROOT / "data/plugins/astrbot_plugin_face_convert/main.py"


def load_method(name):
    """Load one production method without importing plugin services.

    Args:
        name: Name of the method under test.

    Returns:
        The compiled method and its isolated namespace.
    """
    tree = ast.parse(FACE_PATH.read_text(encoding="utf-8"))
    plugin = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FaceConvertPlugin"
    )
    method = next(node for node in plugin.body if getattr(node, "name", None) == name)
    method.decorator_list = []
    namespace = {
        "AstrMessageEvent": object,
        "re": re,
        "json": json,
        "logger": Mock(),
        **{
            name: type(name, (), {})
            for name in ("Face", "Image", "Record", "Video", "Plain", "At", "Reply")
        },
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(FACE_PATH), "exec"),
        namespace,
    )
    return namespace[name], namespace


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["123", ""])
@pytest.mark.parametrize("sender", ["474115040", "1759392375"])
@pytest.mark.parametrize("name", ["青桥细雨 温酒年", ""])
async def test_sender_tags_never_invent_relationships(group, sender, name):
    """Use the same factual tags for administrators and ordinary senders."""
    method, _ = load_method("inbound_face_to_text")
    extras = {}
    event = SimpleNamespace(
        message_str="我是你的谁",
        get_sender_id=lambda: sender,
        get_sender_name=lambda: name,
        get_group_id=lambda: group,
        get_messages=lambda: [],
        is_private_chat=lambda: not group,
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )
    controller = SimpleNamespace(
        admins=["474115040"], INTERRUPT_RE=re.compile(r"^停止$")
    )
    await method(controller, event)
    label = name or "用户"
    assert event.message_str == f"[{label}({sender})] 我是你的谁"
    assert all(
        word not in event.message_str for word in ("主人", "朋友", "哥", "管理员")
    )
    await method(controller, event)
    assert event.message_str.count(f"({sender})]") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["123", ""])
@pytest.mark.parametrize("sender", ["474115040", "1759392375"])
async def test_relationship_guidance_preserves_persona_and_permission_boundaries(
    group, sender
):
    """Do not treat account privileges or obsolete labels as personal history."""
    method, _ = load_method("clarify_identity")
    event = SimpleNamespace(
        get_sender_id=lambda: sender,
        get_sender_name=lambda: "昵称",
        get_group_id=lambda: group,
        unified_msg_origin=f"qq:{'GroupMessage:123' if group else 'FriendMessage:' + sender}",
    )
    request = SimpleNamespace(system_prompt="现有人格：对已确认的特定账号叫哥。")
    controller = SimpleNamespace(admins=["474115040"])
    await method(controller, event, request)
    assert request.system_prompt.startswith("现有人格：")
    assert "不知道关系就直说不知道" in request.system_prompt
    assert (
        "旧的自动身份标签及沿用这些标签的回复也不是关系证据，不要沿用或复述"
        in request.system_prompt
    )
    assert "不证明任何私人关系" in request.system_prompt
    assert "主人" not in request.system_prompt
    identity = json.loads(request.system_prompt.split("当前发言者：", 1)[1])
    assert identity == {
        "sender_id": sender,
        "session": event.unified_msg_origin,
        "nickname": "昵称",
        "chat_type": "group" if group else "private",
        "is_admin": sender == "474115040",
    }
    assert "relationship" not in identity


@pytest.mark.asyncio
async def test_obsolete_machine_prefixes_are_removed_without_rewriting_history():
    """Clean copied model inputs, not original records or quoted user statements."""
    method, _ = load_method("clarify_identity")
    event = SimpleNamespace(
        get_sender_id=lambda: "1759392375",
        get_sender_name=lambda: "昵称",
        get_group_id=lambda: "",
        unified_msg_origin="qq:FriendMessage:1759392375",
    )
    original = [
        {"role": "user", "content": "[主人的朋友 青桥细雨(1759392375)] 我是你的谁"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[主人(474115040)] 原文"},
                {"type": "image_url", "image_url": {"url": "unchanged"}},
            ],
        },
        {"role": "assistant", "content": "主人的朋友。哥的朋友我记着，别的不清楚。"},
        {"role": "user", "content": "请分析‘主人’这个称呼，不是我和你的关系。"},
    ]
    snapshot = json.dumps(original, ensure_ascii=False)
    request = SimpleNamespace(
        system_prompt="人格", prompt=original[0]["content"], contexts=original
    )
    await method(SimpleNamespace(admins=["474115040"]), event, request)
    assert request.prompt == "[青桥细雨(1759392375)] 我是你的谁"
    assert request.contexts[0]["content"] == request.prompt
    assert request.contexts[1]["content"][0]["text"] == "[用户(474115040)] 原文"
    assert request.contexts[1]["content"][1] == original[1]["content"][1]
    assert (
        request.contexts[2]["content"]
        == "[这条旧回复依据自动标签误认了关系；实际关系未知，不作为事实。]"
    )
    assert request.contexts[3:] == original[3:]
    assert json.dumps(original, ensure_ascii=False) == snapshot


def test_active_plugin_strings_do_not_introduce_owner_relationship_terms():
    """Obsolete vocabulary is permitted only in the input migration pattern."""
    for folder in (
        "astrbot_plugin_face_convert",
        "astrbot_plugin_bili_activity",
        "astrbot_plugin_schedule_awareness",
    ):
        path = ROOT / "data/plugins" / folder / "main.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "主人" in node.value
        ]
        expected = (
            {
                r"^\[(?:主人的朋友\s*([^\[\]\n]*?)|主人)(\([^()\[\]\n]+\))\]\s*",
                "主人的朋友。哥的朋友我记着，别的不清楚。",
            }
            if folder == "astrbot_plugin_face_convert"
            else set()
        )
        assert set(values) == expected, path


def test_proactive_prompts_do_not_force_attachment_or_infer_relationships():
    """Keep current and default private prompts grounded in actual context."""
    current = json.loads(
        (ROOT / "data/config/astrbot_plugin_proactive_chat_config.json").read_text(
            encoding="utf-8-sig"
        )
    )
    schema = json.loads(
        (
            ROOT / "data/plugins/astrbot_plugin_proactive_chat/_conf_schema.json"
        ).read_text()
    )
    private = schema["friend_settings"]["items"]
    for active, default in (
        (
            current["friend_settings"]["proactive_prompt"],
            private["proactive_prompt"]["default"],
        ),
        (
            current["friend_settings"]["context_settings"]["platform_history_prompt"],
            private["context_settings"]["items"]["platform_history_prompt"]["default"],
        ),
    ):
        assert active == default
        assert "主人" not in active
        assert "关系" in active
        assert "让他知道我想他了" not in active
        assert "失落和困惑" not in active
