"""Verify the real AstrBot local-tool executor delivers the generated image once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from data.plugins.astrbot_plugin_sys_tools.main import SysToolsPlugin
from data.plugins.astrbot_plugin_sys_tools.status_collector import collect_status


class CaptureEvent(AstrMessageEvent):
    def __init__(self):
        msg = AstrBotMessage()
        msg.type = MessageType.FRIEND_MESSAGE
        msg.self_id = "test-bot"
        msg.sender = MessageMember("test-user", "test")
        msg.message = []
        msg.message_str = "状态"
        super().__init__(
            "状态", msg, PlatformMetadata("webchat", "test", "test"), "test"
        )
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        # The real platform reads/converts the file during this awaited send.
        assert len(message.chain) == 1
        assert message.chain[0].type.name == "Image"
        data = await message.chain[0].convert_to_base64()
        assert len(data) > 1000


def test_real_executor_sends_one_status_image(monkeypatch, tmp_path):
    async def run():
        plugin = SysToolsPlugin(SimpleNamespace(get_platform_inst=lambda _: None), {})
        data = collect_status(top_n=2, show_gpu=False, sample_seconds=0.1)
        data.update(
            network_connection=[], astrbot_version="test", plugin_version="test"
        )
        plugin.build_status_data = AsyncMock(return_value=data)
        monkeypatch.setattr(
            "data.plugins.astrbot_plugin_sys_tools.main.get_astrbot_temp_path",
            lambda: str(tmp_path),
        )
        event = CaptureEvent()
        tool = FunctionTool(
            name="query_server_status",
            description="status image",
            parameters={"type": "object", "properties": {}},
            handler=plugin.query_server_status,
        )
        context = ContextWrapper(SimpleNamespace(event=event))
        try:
            results = [
                value async for value in FunctionToolExecutor.execute(tool, context)
            ]
            assert results == [None]
            assert len(event.sent) == 1
            assert event.sent[0].type == "tool_direct_result"
            assert len(event._temporary_local_files) == 1
            event.cleanup_temporary_local_files()
            assert not list(tmp_path.rglob("status-*.png"))
        finally:
            await plugin.terminate()

    asyncio.run(run())
