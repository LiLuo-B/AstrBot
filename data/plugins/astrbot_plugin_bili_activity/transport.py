"""PhoneMCP image transfer and OneBot receipts, with no account credentials in logs."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import re
import time
import uuid
import warnings
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


class TransportError(RuntimeError):
    """Public error messages deliberately exclude raw responses and credentials."""


class PhoneBusy(TransportError):
    pass


class DeliveryUnknown(TransportError):
    """The server may have accepted a send. Do not automatically send it again."""


class DeliveryRejected(TransportError):
    """OneBot explicitly rejected the send."""


class MCPClient:
    """Open/use/close MCP in one task; never share an AnyIO cancel scope across tasks."""

    def __init__(self, url: str, transport: str = "sse", timeout: float = 45):
        if transport not in {"sse", "streamable-http", "streamable_http"}:
            raise ValueError("unsupported MCP transport")
        self.url, self.transport, self.timeout = url, transport, timeout

    async def call(self, name: str, args: dict | None = None):
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with asyncio.timeout(self.timeout):
                if self.transport == "sse":
                    connection = sse_client(
                        self.url, timeout=min(self.timeout, 15),
                        sse_read_timeout=self.timeout,
                    )
                else:
                    connection = streamablehttp_client(
                        self.url, timeout=self.timeout,
                        sse_read_timeout=self.timeout,
                    )
                async with connection as streams:
                    async with ClientSession(
                        streams[0], streams[1],
                        read_timeout_seconds=timedelta(seconds=self.timeout),
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(name, args or {})
                if getattr(result, "isError", False):
                    raise TransportError(f"MCP tool {name} reported an error")
                return result
        except asyncio.CancelledError:
            raise
        except TransportError:
            raise
        except Exception as exc:
            # Upstream exceptions can contain URLs, cookies or full image payloads.
            raise TransportError(f"MCP {name} failed ({type(exc).__name__})") from None

    async def json_call(self, name: str, args: dict | None = None):
        result = await self.call(name, args)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            # FastMCP wraps scalar/list return values in {"result": ...}.
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        texts = [
            item.text for item in getattr(result, "content", [])
            if getattr(item, "type", None) == "text"
        ]
        if len(texts) != 1:
            raise TransportError(f"MCP {name} returned no unambiguous JSON value")
        try:
            return json.loads(texts[0])
        except (TypeError, ValueError):
            raise TransportError(f"MCP {name} returned invalid JSON") from None


class PhoneGate:
    """One lease for a complete navigation sequence, one command at a time.

    A chat task sees PhoneBusy immediately while a background lease exists.
    The caller may explicitly cancel/await its own background task, then retry.
    Raw PhoneMCP tools must be removed from the agent toolset so they cannot bypass
    this gate. An owner token is application state, never a model argument.
    """

    def __init__(self):
        self._owner: str | None = None
        self._command = asyncio.Lock()

    @property
    def owner(self):
        return self._owner

    @asynccontextmanager
    async def lease(self, owner: str | None = None):
        if self._owner is not None or self._command.locked():
            raise PhoneBusy("手机正由另一项任务使用")
        token = owner or uuid.uuid4().hex
        self._owner = token
        try:
            yield token
        finally:
            # Await any in-flight command before letting another lease navigate.
            async with self._command:
                if self._owner == token:
                    self._owner = None

    async def call(self, client, name: str, args: dict | None = None, owner=None):
        if self._owner is not None and owner != self._owner:
            raise PhoneBusy("手机正由另一项任务使用")
        if owner is not None and owner != self._owner:
            raise PhoneBusy("手机活动租约已结束")
        # Foreign unleased commands do not queue behind each other indefinitely.
        if self._command.locked() and owner is None:
            raise PhoneBusy("手机正在执行操作")
        async with self._command:
            if self._owner is not None and owner != self._owner:
                raise PhoneBusy("手机正由另一项任务使用")
            return await client.call(name, args or {})


@dataclass(frozen=True)
class Snapshot:
    path: str
    fingerprint: str
    captured_at: float
    request_started_at: float
    device_id: str
    width: int
    height: int
    size: int
    mime_type: str
    video: dict | None = None
    video_verified: bool = False

    def metadata(self):
        return asdict(self)


def image_bytes(result, max_input_bytes=12_000_000):
    """Require actual MCP image content. A screenshot path/text is not an image."""
    images = [
        item for item in getattr(result, "content", [])
        if getattr(item, "type", None) == "image"
    ]
    if len(images) != 1:
        raise TransportError("PhoneMCP 未返回唯一的真实截图图片")
    item = images[0]
    if item.mimeType not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise TransportError("PhoneMCP 截图格式不受支持")
    encoded = item.data
    if not isinstance(encoded, str) or len(encoded) > (max_input_bytes + 2) // 3 * 4:
        raise TransportError("PhoneMCP 截图超过大小限制")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise TransportError("PhoneMCP 截图编码无效") from None
    if not raw or len(raw) > max_input_bytes:
        raise TransportError("PhoneMCP 截图为空或超过大小限制")
    return raw


def prepare_image(raw: bytes, target_bytes=350_000, hard_limit=1_000_000):
    """Keep native pixels. Preserve already-small JPEG/PNG, otherwise JPEG q>=78.

    There is a hard transfer limit rather than silently making tiny unreadable
    pictures to meet a byte target. Fingerprints ignore the Android status bar
    and bottom navigation strip, but are based on decoded pixels, not filenames.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 12_000_000:
                    raise ValueError("image pixel limit")
                if source.width < 64 or source.height < 64:
                    raise ValueError("image too small")
                source.load()
                fmt = source.format
                image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise TransportError("PhoneMCP 返回的截图无法解码或尺寸异常") from None

    width, height = image.size
    # Older PhoneMCP returned a synthetic black image on ADB failure. A uniformly
    # black screen is also not useful evidence of current content. Fail closed
    # until the capture implementation reports an actual readable screen.
    if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise TransportError("截图为全黑画面，无法确认是否为PhoneMCP失败占位图")
    # Exact content pixels with only small system bars excluded. This will not
    # confuse different comments/frames as a tiny perceptual hash could.
    top = min(42, height // 24)
    bottom = min(32, height // 32)
    crop = image.crop((0, top, width, height - bottom))
    fingerprint = hashlib.sha256(
        f"{width}x{height}:".encode() + crop.tobytes()
    ).hexdigest()
    if fmt in {"JPEG", "PNG"} and len(raw) <= target_bytes:
        return raw, "image/jpeg" if fmt == "JPEG" else "image/png", fingerprint, width, height

    output = b""
    for quality in (92, 88, 84, 80, 78):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=0)
        output = buf.getvalue()
        if len(output) <= target_bytes:
            break
    if len(output) > hard_limit:
        raise TransportError("截图过大；为保持文字可读，本次未发送")
    return output, "image/jpeg", fingerprint, width, height


class ScreenCapture:
    def __init__(
        self, client, folder, device_id="emulator-5554",
        lock: PhoneGate | None = None, max_age=30,
    ):
        if device_id != "emulator-5554":
            raise ValueError("this deployment only permits emulator-5554")
        self.client, self.device_id, self.lock = client, device_id, lock
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age

    async def capture(self, video=None, owner=None):
        started = time.time()
        if self.lock:
            result = await self.lock.call(
                self.client, "get_screenshot", {"device_id": self.device_id}, owner=owner,
            )
        else:
            result = await self.client.call("get_screenshot", {"device_id": self.device_id})
        received = time.time()
        if received - started > self.max_age:
            raise TransportError("截图获取耗时过长，不能作为当前画面分享")
        raw = image_bytes(result)
        encoded, mime, fingerprint, width, height = await asyncio.to_thread(prepare_image, raw)
        suffix = ".png" if mime == "image/png" else ".jpg"
        filename = f"{int(received * 1000)}-{uuid.uuid4().hex[:12]}{suffix}"
        path = self.folder / filename
        # No supplied Windows path is used. This is a cloud-owned file.
        with path.open("xb") as stream:
            stream.write(encoded)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        snapshot = Snapshot(
            str(path.resolve()), fingerprint, received, started, self.device_id,
            width, height, len(encoded), mime, video, False,
        )
        metadata_path = path.with_suffix(path.suffix + ".json")
        with metadata_path.open("x", encoding="utf-8") as stream:
            json.dump(snapshot.metadata(), stream, ensure_ascii=False, indent=2)
        return snapshot

    def cleanup(self, keep_seconds=86400):
        """Only remove this helper's generated screenshot files and their metadata."""
        cutoff = time.time() - keep_seconds
        for path in self.folder.iterdir():
            if (
                re.fullmatch(r"\d+-[0-9a-f]{12}\.(?:png|jpg)(?:\.json)?", path.name)
                and path.is_file() and path.stat().st_mtime < cutoff
            ):
                path.unlink()


def _onebot_data(value):
    if not isinstance(value, dict):
        raise TransportError("OneBot returned no structured response")
    if value.get("status") in {"failed", "error"} or value.get("retcode", 0) not in (0, None):
        raise DeliveryRejected("OneBot 拒绝发送或读取消息")
    return value.get("data", value)


def _message_id(data):
    if not isinstance(data, dict):
        return None
    value = data.get("message_id")
    if isinstance(value, bool) or value in (None, "", 0, "0"):
        return None
    return str(value)


def _image_present(message):
    if isinstance(message, list):
        return any(
            isinstance(segment, dict) and segment.get("type") == "image"
            and isinstance(segment.get("data"), dict)
            and bool(segment["data"].get("file") or segment["data"].get("url"))
            for segment in message
        )
    if isinstance(message, str):
        return re.search(r"\[CQ:image,[^\]]*(?:file|url)=[^\],]+", message) is not None
    return False


class QQDelivery:
    """Use AstrBot's existing OneBot bot and inspect its actual send response."""

    def __init__(self, context, owner_scope: str, timeout=20):
        self.context, self.owner_scope, self.timeout = context, owner_scope, timeout
        self._target(owner_scope)

    def _target(self, scope):
        pieces = scope.split(":", 2)
        if (
            scope != self.owner_scope or len(pieces) != 3
            or pieces[1] != "FriendMessage" or not pieces[2].isdigit()
        ):
            raise PermissionError("截图和主动消息仅允许发送到配置的哥的私聊")
        return pieces[0], int(pieces[2])

    async def send(self, scope: str, text: str, image_path=None):
        platform_id, target = self._target(scope)
        platform = self.context.get_platform_inst(platform_id)
        bot = getattr(platform, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise TransportError("未找到可用的 QQ OneBot 连接")
        message = []
        if text:
            message.append({"type": "text", "data": {"text": str(text)[:2000]}})
        if image_path is not None:
            path = Path(image_path)
            if not path.is_absolute() or not path.is_file() or path.stat().st_size > 1_000_000:
                raise TransportError("云端截图文件无效或超过发送大小限制")
            raw = await asyncio.to_thread(path.read_bytes)
            # Reject accidental text/base64/path files before sending.
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    image.verify()
            except (OSError, ValueError):
                raise TransportError("待发送文件不是可打开的图片") from None
            message.append({
                "type": "image",
                "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")},
            })
        if not message:
            raise ValueError("empty message")

        try:
            response = await asyncio.wait_for(
                bot.call_action("send_private_msg", user_id=target, message=message),
                self.timeout,
            )
            data = _onebot_data(response)
        except asyncio.CancelledError:
            raise
        except DeliveryRejected:
            raise
        except Exception as exc:
            raise DeliveryUnknown(
                f"QQ 发送结果不确定（{type(exc).__name__}），已停止自动重发"
            ) from None
        mid = _message_id(data)
        if not mid:
            raise DeliveryUnknown("QQ 未返回 message_id，已停止自动重发")
        receipt = {
            "message_id": mid, "scope": scope, "acknowledged": True,
            "sent_at": time.time(), "image_readback": False,
            "readback_verified": False, "verified": False,
            "recipient_confirmed": False,
        }
        # GETs are read-only and can be retried once; the send above cannot.
        for attempt in range(2):
            try:
                value = await asyncio.wait_for(
                    bot.call_action("get_msg", message_id=int(mid) if mid.lstrip("-").isdigit() else mid),
                    self.timeout,
                )
                readback = _onebot_data(value)
                if not isinstance(readback, dict) or _message_id(readback) != mid:
                    receipt["readback_error"] = "message_id_mismatch"
                    break
                if readback.get("message_type") not in (None, "private"):
                    receipt["readback_error"] = "unexpected_message_type"
                    break
                # NapCat versions differ in which destination field is exposed.
                destination = readback.get("target_id", readback.get("peer_id"))
                if destination is not None and str(destination) != str(target):
                    receipt["readback_error"] = "recipient_mismatch"
                    break
                receipt["image_readback"] = _image_present(readback.get("message"))
                receipt["readback_verified"] = (
                    image_path is None or receipt["image_readback"]
                )
                receipt["verified"] = receipt["readback_verified"]
                receipt["readback_target_verified"] = destination is not None
                if not receipt["verified"]:
                    receipt["readback_error"] = "image_segment_missing"
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                receipt["readback_error"] = type(exc).__name__
                if attempt == 0:
                    await asyncio.sleep(0.25)
        # verified refers to the OneBot record. Only the user's client can
        # establish that the delivered picture actually opens on their QQ.
        return receipt
