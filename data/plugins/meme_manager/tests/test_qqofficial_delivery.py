import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.main import MemeSender
from astrbot_plugin_meme_manager.mixins import event_handlers
from astrbot_plugin_meme_manager.mixins.event_handlers import (
    LLM_REQUEST_ORIGIN_EXTRA_KEY,
)
from PIL import Image as PillowImage

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.message_type import MessageType


class FakeGroupMessage:
    pass


class FakeGroupMessageChild(FakeGroupMessage):
    pass


class FakeChannelMessage:
    group_id = "channel-group"


class FakeC2CMessage:
    pass


class FakeDirectMessage:
    pass


@pytest.fixture
def fake_botpy_messages(monkeypatch):
    message_module = ModuleType("botpy.message")
    message_module.GroupMessage = FakeGroupMessage
    message_module.Message = FakeChannelMessage
    message_module.C2CMessage = FakeC2CMessage
    message_module.DirectMessage = FakeDirectMessage
    botpy_module = ModuleType("botpy")
    botpy_module.message = message_module
    monkeypatch.setitem(sys.modules, "botpy", botpy_module)
    monkeypatch.setitem(sys.modules, "botpy.message", message_module)
    return SimpleNamespace(
        group=FakeGroupMessage(),
        group_child=FakeGroupMessageChild(),
        channel=FakeChannelMessage(),
        c2c=FakeC2CMessage(),
        direct=FakeDirectMessage(),
    )


@pytest.fixture
def delivery_runtime(tmp_path, fake_botpy_messages):
    meme_path = tmp_path / "memes" / "happy" / "meme.png"
    meme_path.parent.mkdir(parents=True)
    PillowImage.new("RGB", (2, 2)).save(meme_path)

    sender = object.__new__(MemeSender)
    sender.context = SimpleNamespace(send_message=AsyncMock())
    sender.convert_static_to_gif = False
    sender.send_image_as_base64 = False
    sender.emotions_probability = 100
    sender.max_memes_per_message = -1
    sender.enable_mixed_message = False
    sender.mixed_message_probability = 100
    sender.streaming_compatibility = True
    sender.filter_all_tags = False
    sender.content_cleanup_rule = r"&&[a-zA-Z]*&&"
    sender._resolve_runtime_pack_context = lambda **kwargs: {
        "pack_dirs": {"pack": tmp_path},
        "pack_dir": tmp_path,
    }
    sender._get_runtime_memes_dir_for_event = lambda event: tmp_path

    state = {
        LLM_REQUEST_ORIGIN_EXTRA_KEY: "chat",
        "platform": "qq_official",
        "message_type": MessageType.GROUP_MESSAGE,
        "meme_manager_semantic_active": False,
    }
    sender._semantic_mode_active = lambda event: bool(
        state.get("meme_manager_semantic_active")
    )

    event = SimpleNamespace(
        get_extra=lambda key: state.get(key),
        set_extra=lambda key, value: state.update({key: value}),
        get_result=lambda: state.get("result"),
        get_platform_name=lambda: state["platform"],
        get_message_type=lambda: state["message_type"],
        message_obj=SimpleNamespace(raw_message=fake_botpy_messages.group),
        unified_msg_origin="qq_official:group:123",
        send=AsyncMock(),
    )
    return sender, event, state, meme_path, fake_botpy_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_name", ["group", "group_child"])
async def test_qq_group_delivers_through_event_send(delivery_runtime, raw_name):
    sender, event, _, meme_path, raw_messages = delivery_runtime
    event.message_obj.raw_message = getattr(raw_messages, raw_name)
    image = Image.fromFileSystem(str(meme_path))

    await sender._send_meme_image(event, image)

    event.send.assert_awaited_once()
    sender.context.send_message.assert_not_awaited()
    assert event.send.await_args.args[0].chain == [image]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_name", ["channel", "c2c", "direct", "missing"])
async def test_qq_group_message_type_requires_botpy_group_raw(
    delivery_runtime, raw_name
):
    sender, event, _, meme_path, raw_messages = delivery_runtime
    if raw_name == "missing":
        event.message_obj = SimpleNamespace()
    else:
        event.message_obj.raw_message = getattr(raw_messages, raw_name)
    image = Image.fromFileSystem(str(meme_path))

    await sender._send_meme_image(event, image)

    event.send.assert_not_awaited()
    sender.context.send_message.assert_awaited_once()
    assert sender.context.send_message.await_args.args[0] == event.unified_msg_origin
    assert sender.context.send_message.await_args.args[1].chain == [image]


@pytest.mark.asyncio
async def test_qq_official_non_group_type_keeps_context_route(delivery_runtime):
    sender, event, state, meme_path, raw_messages = delivery_runtime
    state["message_type"] = MessageType.FRIEND_MESSAGE
    event.message_obj.raw_message = raw_messages.group
    image = Image.fromFileSystem(str(meme_path))

    await sender._send_meme_image(event, image)

    event.send.assert_not_awaited()
    sender.context.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_name", "uses_event_send"),
    [
        ("gewechat", True),
        ("webchat", True),
        ("telegram", False),
        ("aiocqhttp", False),
    ],
)
async def test_non_qq_platforms_keep_existing_routes_without_botpy(
    delivery_runtime, monkeypatch, platform_name, uses_event_send
):
    sender, event, state, meme_path, raw_messages = delivery_runtime
    state["platform"] = platform_name
    event.message_obj.raw_message = raw_messages.group
    monkeypatch.setitem(sys.modules, "botpy", None)
    monkeypatch.setitem(sys.modules, "botpy.message", None)
    image = Image.fromFileSystem(str(meme_path))

    await sender._send_meme_image(event, image)

    if uses_event_send:
        event.send.assert_awaited_once()
        sender.context.send_message.assert_not_awaited()
    else:
        event.send.assert_not_awaited()
        sender.context.send_message.assert_awaited_once()
        assert (
            sender.context.send_message.await_args.args[0] == event.unified_msg_origin
        )


@pytest.mark.asyncio
async def test_missing_botpy_keeps_context_route_for_qq_group(
    delivery_runtime, monkeypatch
):
    sender, event, _, meme_path, _ = delivery_runtime
    monkeypatch.setitem(sys.modules, "botpy", None)
    monkeypatch.setitem(sys.modules, "botpy.message", None)
    image = Image.fromFileSystem(str(meme_path))

    await sender._send_meme_image(event, image)

    event.send.assert_not_awaited()
    sender.context.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_qq_group_send_failure_does_not_retry_other_route(delivery_runtime):
    sender, event, _, meme_path, _ = delivery_runtime
    event.send.side_effect = RuntimeError("send failed")
    image = Image.fromFileSystem(str(meme_path))

    with pytest.raises(RuntimeError, match="send failed"):
        await sender._send_meme_image(event, image)

    event.send.assert_awaited_once()
    sender.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_message_sent_delivers_converted_image_to_qq_group(
    delivery_runtime,
):
    sender, event, state, meme_path, _ = delivery_runtime
    sender.send_image_as_base64 = True
    state["meme_manager_pending_images"] = [Image.fromFileSystem(str(meme_path))]

    await sender._after_message_sent_impl(event)

    event.send.assert_awaited_once()
    sender.context.send_message.assert_not_awaited()
    sent_image = event.send.await_args.args[0].chain[0]
    assert sent_image.file.startswith("base64://")


@pytest.mark.asyncio
async def test_streaming_legacy_delivers_through_event_send(delivery_runtime):
    sender, event, state, _, _ = delivery_runtime
    state["result"] = MessageEventResult(
        chain=[Plain("hi")],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    state["found_emotions"] = ["happy"]
    state["meme_manager_legacy_probability_hit"] = True

    await sender._send_memes_streaming(event)

    event.send.assert_awaited_once()
    sender.context.send_message.assert_not_awaited()
    assert event.send.await_args.args[0].chain[0].file.endswith("meme.png")
    assert state["found_emotions"] is None


@pytest.mark.asyncio
async def test_streaming_semantic_delivers_through_event_send(
    delivery_runtime, monkeypatch
):
    sender, event, state, meme_path, _ = delivery_runtime
    selected_id = "meme:0123456789ab"
    state["result"] = MessageEventResult(
        chain=[Plain("hi")],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    state["meme_manager_semantic_selected_ids"] = [selected_id]
    state["meme_manager_semantic_active"] = True
    monkeypatch.setattr(
        event_handlers,
        "validate_selected_id",
        lambda event, value, pack: meme_path if value == selected_id else None,
    )

    await sender._send_memes_streaming(event)

    event.send.assert_awaited_once()
    sender.context.send_message.assert_not_awaited()
    assert event.send.await_args.args[0].chain[0].file.endswith("meme.png")
    assert state["meme_manager_semantic_selected_ids"] is None


@pytest.mark.asyncio
async def test_merged_reply_does_not_add_supplementary_sends(delivery_runtime):
    sender, event, state, _, _ = delivery_runtime
    sender.enable_mixed_message = True
    result = MessageEventResult(
        chain=[Plain("hi")],
        result_content_type=ResultContentType.LLM_RESULT,
    )
    state["result"] = result
    state["found_emotions"] = ["happy"]
    state["meme_manager_legacy_probability_hit"] = True

    await sender._on_decorating_result_impl(event)

    assert any(isinstance(component, Image) for component in result.chain)
    assert not state.get("meme_manager_pending_images")
    await sender._after_message_sent_impl(event)
    event.send.assert_not_awaited()
    sender.context.send_message.assert_not_awaited()
