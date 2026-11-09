"""Keep event status and attribution across custom summary and recall prompts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.prompts import prompt_manager
from astrbot_plugin_livingmemory.core.prompts.evidence_rules import (
    INJECTION_EVIDENCE_REMINDER,
    RECALL_EVIDENCE_RULES,
    SUMMARY_EVIDENCE_RULES,
)
from astrbot_plugin_livingmemory.core.utils.formatting import (
    format_memories_for_injection,
)


@pytest.fixture
def prompts(tmp_path, monkeypatch):
    """Install isolated custom templates without touching deployment prompts."""
    manager = prompt_manager.PromptManager(str(tmp_path))
    monkeypatch.setattr(prompt_manager, "_prompt_manager", manager)
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("persona", [None, "persona"])
@pytest.mark.parametrize("manager_available", [False, True])
async def test_custom_summary_still_receives_event_evidence_rules(
    prompts, group, persona, manager_available, monkeypatch
):
    prompts.update_prompt("private_chat_prompt", "CUSTOM PRIVATE {conversation}")
    prompts.update_prompt("group_chat_prompt", "CUSTOM GROUP {conversation}")
    prompts.update_prompt("memory_system_prompt_base", "CUSTOM SYSTEM")
    prompts.update_prompt(
        "memory_system_prompt_with_persona", "CUSTOM PERSONA {persona_prompt}"
    )
    if not manager_available:
        monkeypatch.setattr(prompt_manager, "_prompt_manager", None)
    context = SimpleNamespace(
        persona_manager=SimpleNamespace(
            get_persona=AsyncMock(
                return_value=SimpleNamespace(system_prompt="保持自然活泼的语气")
            )
        )
    )
    processor = MemoryProcessor(context=context, llm_provider=Mock())
    processor._call_llm_with_retry = AsyncMock(
        return_value='{"summary":"张三计划明天再烤，我之前夸过但没有成品依据", "topics":["烧鹅"], "key_facts":["张三计划再烤"], "participants":["张三","我"], "sentiment":"neutral", "importance":0.6}'
    )
    messages = [
        Message(
            id=1,
            session_id="test",
            role="user",
            content="明天再烤，今天先腌着",
            sender_id="u1",
            sender_name="张三",
            group_id="g1" if group else None,
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="test",
            role="assistant",
            content="你烤得挺像样",
            sender_id="bot",
            sender_name="Bot",
            group_id="g1" if group else None,
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]
    await processor.process_conversation(
        messages, is_group_chat=group, persona_id=persona
    )
    kwargs = processor._call_llm_with_retry.await_args.kwargs
    assert SUMMARY_EVIDENCE_RULES in kwargs["system_prompt"]
    assert kwargs["system_prompt"].count("[Event evidence boundaries]") == 1
    if manager_available:
        assert (
            "CUSTOM PERSONA" in kwargs["system_prompt"]
            if persona
            else "CUSTOM SYSTEM" in kwargs["system_prompt"]
        )
    if persona:
        assert "保持自然活泼的语气" in kwargs["system_prompt"]
    assert "明天再烤" in kwargs["prompt"]
    assert "Bot:" in kwargs["prompt"]


@pytest.mark.asyncio
async def test_merge_keeps_source_times_and_unresolved_reports(prompts):
    processor = MemoryProcessor(llm_provider=Mock())
    processor._call_llm_with_retry = AsyncMock(
        return_value='{"summary":"张三原计划再烤，后来取消；机器人的夸赞未获确认", "key_facts":[], "topics":["烧鹅"], "importance":0.5}'
    )
    await processor.merge_memories(
        [
            {
                "content": "张三计划明天烤",
                "metadata": {
                    "source_time_start": "2026-11-08",
                    "source_time_end": "2026-11-08",
                },
            },
            {
                "content": "张三后来取消；我曾说烤得不错",
                "metadata": {
                    "source_time_start": "2026-11-09",
                    "source_time_end": "2026-11-09",
                },
            },
        ]
    )
    prompt, system = processor._call_llm_with_retry.await_args.args
    assert SUMMARY_EVIDENCE_RULES in system
    assert "消除相互矛盾" not in system
    assert (
        "source_time_start" in prompt
        and "2026-11-08" in prompt
        and "2026-11-09" in prompt
    )


@pytest.mark.parametrize("manager_available", [False, True])
def test_auto_injection_keeps_persona_text_but_not_completed_event_assumption(
    prompts, manager_available, monkeypatch
):
    prompts.update_prompt(
        "memory_injection_header",
        "These are PAST records — they already happened and are NOT part of the current conversation.",
    )
    prompts.update_prompt("memory_injection_footer", "CUSTOM FOOTER")
    if not manager_available:
        monkeypatch.setattr(prompt_manager, "_prompt_manager", None)
    result = format_memories_for_injection(
        [
            {
                "content": "张三计划明天再烤",
                "metadata": {
                    "persona_summary": "我记得张三说要明天再烤呀",
                    "key_facts": ["张三计划再烤"],
                },
            }
        ]
    )
    assert "我记得张三说要明天再烤呀" in result
    assert "they already happened" not in result
    assert INJECTION_EVIDENCE_REMINDER in result
    if manager_available:
        assert "CUSTOM FOOTER" in result
    assert format_memories_for_injection([]) == ""


@pytest.mark.asyncio
async def test_custom_agent_policy_cannot_omit_event_evidence_rules(prompts):
    from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import (
        MemoryRecall,
    )

    prompts.update_prompt("agent_recall_policy", "CUSTOM RECALL {available_tools}")
    recall = MemoryRecall.__new__(MemoryRecall)
    recall.config_manager = ConfigManager(
        {
            "agent_tools": {
                "enable_recall_tool": True,
                "enable_agentic_recall_beta": True,
            }
        }
    )
    recall.memory_engine = Mock()
    request = SimpleNamespace(
        func_tool=SimpleNamespace(names=lambda: ["recall_long_term_memory"]),
        extra_user_content_parts=[],
    )
    recall._maybe_inject_agent_recall_policy(request, "test")
    assert len(request.extra_user_content_parts) == 1
    text = request.extra_user_content_parts[0].text
    assert "CUSTOM RECALL" in text and RECALL_EVIDENCE_RULES in text
    assert "- read_memory_evidence:" not in text
    assert getattr(request.extra_user_content_parts[0], "_no_save", False)
