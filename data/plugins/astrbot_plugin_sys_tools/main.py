"""LLM tools for host IP lookup and the original PicStatus-style status card."""

from __future__ import annotations

import asyncio
import ipaddress
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from astrbot import __version__ as astrbot_version
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

from .status_assets import StatusAssets
from .status_collector import check_connectivity, collect_status, format_status_text

PLUGIN_VERSION = "v1.2.0"
COMPONENTS = ["header", "cpu_mem", "gpu", "disk", "network", "process", "footer"]
DISPLAY_DEFAULTS = {
    "ignore_parts": [],
    "ignore_bad_parts": False,
    "sort_parts": True,
    "sort_parts_reverse": False,
    "ignore_disk_ios": [r"^(loop|zram)\d*$"],
    "ignore_no_io_disk": False,
    "sort_disk_ios": True,
    "ignore_nets": [r"^lo(op)?\d*$|^(Loopback|本地连接)", r"^(veth|br-|tun|tap)"],
    "ignore_0b_net": False,
    "sort_nets": True,
    "ignore_procs": [r"^System Idle Process$"],
    "proc_cpu_max_100p": False,
}
IP_ENDPOINTS = ("https://ip.3322.net", "https://api.ipify.org", "https://icanhazip.com")
DEFAULT_SITES = [
    {"name": "百度", "url": "https://www.baidu.com/", "use_proxy": False},
    {"name": "Google", "url": "https://www.google.com/", "use_proxy": True},
    {"name": "Pixiv", "url": "https://www.pixiv.net/", "use_proxy": True},
]


@register(
    "astrbot_plugin_sys_tools",
    "cherries",
    "公网 IP 与原版 PicStatus 状态图工具",
    PLUGIN_VERSION,
)
class SysToolsPlugin(Star):
    """Expose host tools without registering a second command responder."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config or {}
        ipc = self.config.get("ip_lookup", {}) or {}
        stc = self.config.get("status", {}) or {}
        self.ip_enabled = bool(ipc.get("enable_public_ip", True))
        self.ip_timeout = max(1, min(15, int(ipc.get("timeout", 10))))
        self.status_enabled = bool(stc.get("enable_server_status", True))
        self.top_n = max(0, min(100, int(stc.get("top_processes", 10))))
        self.show_gpu = bool(stc.get("show_gpu", True))
        self.only_admin = bool(stc.get("only_admin", False))
        self.process_sort_by = stc.get("process_sort_by", "mem")
        self.network_interfaces = stc.get("network_interfaces", []) or None
        self.disk_devices = stc.get("disk_devices", []) or None
        self.test_timeout = max(0.2, min(30, float(stc.get("test_timeout", 5))))
        self.test_sites = stc.get("test_sites", DEFAULT_SITES)
        self.proxy = str(stc.get("proxy", "") or "").strip() or None
        self.sort_sites = bool(stc.get("sort_sites", True))
        self.display_options = {
            key: stc.get(key, default) for key, default in DISPLAY_DEFAULTS.items()
        }
        self.components = list(stc.get("components", COMPONENTS))
        if not self.components or any(
            item not in COMPONENTS for item in self.components
        ):
            raise ValueError(
                "components must contain supported PicStatus section names"
            )
        self.image_format = stc.get("image_format", "png")
        if self.image_format not in {"png", "jpeg"}:
            raise ValueError("image_format must be png or jpeg")
        self.additional_css = list(stc.get("additional_css", []))
        if any(
            item not in {"no-shadow", "no-radius", "no-blur"}
            for item in self.additional_css
        ):
            raise ValueError("additional_css supports no-shadow, no-radius and no-blur")
        self.theme = stc.get("theme", "dark")
        if self.theme not in {"dark", "light"}:
            raise ValueError("theme must be dark or light")
        self.show_current_bot_only = bool(stc.get("show_current_bot_only", True))
        self.nickname = str(stc.get("nickname", "") or "").strip()
        self.use_env_nick = bool(stc.get("use_env_nick", False))
        self._loaded_at = time.time()
        self._counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"received": 0, "sent": 0}
        )
        self._status_lock = asyncio.Lock()
        self._renderer = None
        self._assets = StatusAssets(
            Path(get_astrbot_plugin_data_path()) / "sys_tools" / "assets_cache",
            source=stc.get("background_source", "loli"),
            local_path=stc.get("background_path", ""),
            url=stc.get("background_url", ""),
            proxy=str(stc.get("background_proxy", "") or "").strip() or None,
            timeout=max(0.2, min(30, float(stc.get("background_timeout", 5)))),
            default_avatar=stc.get("default_avatar", ""),
            retry_limit=stc.get("background_retry_limit", 3),
        )

    @filter.on_llm_request()
    async def observe_incoming(self, event: AstrMessageEvent, request: Any) -> None:
        """Count AI requests without waking otherwise ignored group messages.

        Args:
            event: An event whose AI request reached this plugin.
            request: The current provider request, left unchanged.
        """
        if not event.get_extra("_sys_tools_received_counted", False):
            self._counts[event.get_platform_id()]["received"] += 1
            event.set_extra("_sys_tools_received_counted", True)

    @filter.after_message_sent()
    async def observe_reply(self, event: AstrMessageEvent) -> None:
        """Count one observed reply event, not every streaming fragment.

        Args:
            event: The originating event after a successful response pipeline.
        """
        if not event.get_extra("_sys_tools_sent_counted", False):
            self._counts[event.get_platform_id()]["sent"] += 1
            event.set_extra("_sys_tools_sent_counted", True)

    def _bot_card(self, event: AstrMessageEvent, adapter: Any = None) -> dict[str, Any]:
        """Read bot identity and status from the current platform instance.

        Args:
            event: The tool invocation's real delivery context.
            adapter: Optional explicit adapter for an all-platform header.

        Returns:
            Header fields with unknown values explicitly left unset.
        """
        platform_id = (
            adapter.meta().id if adapter is not None else event.get_platform_id()
        )
        explicit_adapter = adapter is not None
        if adapter is None:
            adapter = self.context.get_platform_inst(platform_id)
        # Dashboard previews show the actual primary QQ bot when one is configured.
        if not explicit_adapter and event.get_platform_name() == "webchat":
            manager = getattr(self.context, "platform_manager", None)
            for candidate in getattr(manager, "platform_insts", ()):
                if candidate.meta().name == "qq_official":
                    adapter = candidate
                    platform_id = candidate.meta().id
                    break
        card: dict[str, Any] = {
            "nick": platform_id or "AstrBot",
            "adapter": event.get_platform_name(),
            "avatar": "",
            "bot_connected": None,
            "connected": None,
            "msg_rec": self._counts[platform_id]["received"],
            "msg_sent": self._counts[platform_id]["sent"],
            "stats_scope": "AI 请求/回复 · 插件加载以来",
        }
        if adapter is None:
            if self.use_env_nick and self.nickname:
                card["nick"] = self.nickname
            return card
        try:
            stats = adapter.get_stats()
            started = stats.get("started_at")
            if started:
                start = datetime.fromisoformat(started)
                now = datetime.now(tz=start.tzinfo)
                card["bot_connected"] = max(0, (now - start).total_seconds())
            meta = adapter.meta()
            card["adapter"] = {
                "qq_official": "QQ Official",
                "aiocqhttp": "OneBot V11",
            }.get(meta.name, meta.adapter_display_name or meta.name)
            card["nick"] = (
                adapter.config.get("display_name")
                or adapter.config.get("id")
                or platform_id
            )
            client = adapter.get_client()
            if meta.name == "qq_official":
                robot = getattr(client, "robot", None)
                card["nick"] = getattr(robot, "name", None) or card["nick"]
                card["avatar"] = getattr(robot, "avatar", None) or ""
                sockets = getattr(client, "_active_websockets", ())
                card["connected"] = any(
                    getattr(ws, "_conn", None) is not None
                    and not getattr(ws._conn, "closed", True)
                    and bool(getattr(ws, "_session", {}).get("session_id"))
                    for ws in sockets
                )
            elif meta.name == "aiocqhttp":
                card["connected"] = bool(getattr(client, "_wsr_api_clients", {}))
                clients = getattr(client, "_wsr_api_clients", {})
                self_id = (
                    next(iter(clients), "") if explicit_adapter else event.get_self_id()
                )
                if str(self_id).isdigit():
                    card["avatar"] = f"https://q1.qlogo.cn/g?b=qq&nk={self_id}&s=640"
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            logger.debug(
                "[sys_tools] Platform metadata partially unavailable: %s",
                type(exc).__name__,
            )
        if self.use_env_nick and self.nickname:
            card["nick"] = self.nickname
        return card

    async def build_status_data(self, event: AstrMessageEvent) -> dict[str, Any]:
        """Collect host metrics and resolve assets concurrently within time bounds.

        Args:
            event: Real message context for platform metadata.

        Returns:
            A complete PicStatus template context with no invented metrics.
        """
        cards = [self._bot_card(event)]
        if not self.show_current_bot_only:
            manager = getattr(self.context, "platform_manager", None)
            adapters = [
                adapter
                for adapter in getattr(manager, "platform_insts", ())
                if adapter.meta().name != "webchat"
            ]
            if adapters:
                cards = [self._bot_card(event, adapter) for adapter in adapters]
        host, probes, background, avatars = await asyncio.gather(
            asyncio.to_thread(
                collect_status,
                top_n=self.top_n if "process" in self.components else 0,
                show_gpu=self.show_gpu and "gpu" in self.components,
                process_sort_by=self.process_sort_by,
                network_interfaces=self.network_interfaces
                if "network" in self.components
                else [],
                disk_devices=self.disk_devices if "disk" in self.components else [],
                sample_seconds=0.5,
                display_options=self.display_options,
            ),
            check_connectivity(
                self.test_sites if "network" in self.components else [],
                timeout=self.test_timeout,
                proxy=self.proxy,
            ),
            self._assets.load("background"),
            asyncio.gather(
                *(self._assets.load("avatar", card["avatar"]) for card in cards)
            ),
        )
        for card, avatar in zip(cards, avatars):
            card["avatar"] = avatar
        for probe in probes:
            if probe.get("error"):
                kind = str(probe.get("reason") or "Error")
                probe["display_error"] = "超时" if "timeout" in kind.lower() else kind
        if self.sort_sites:
            # Match upstream: failures sort before successful increasing delays.
            probes.sort(key=lambda item: item["delay"] if item["error"] is None else -1)
        host.update(
            {
                "bots": cards,
                "components": self.components,
                "additional_css": self.additional_css,
                "theme": self.theme,
                "network_connection": probes,
                "background_url": background,
                "astrbot_version": astrbot_version,
                "plugin_version": PLUGIN_VERSION,
                "astrbot_run_time": max(
                    0, time.time() - psutil.Process().create_time()
                ),
                "plugin_run_time": max(0, time.time() - self._loaded_at),
            }
        )
        return host

    @filter.llm_tool(name="query_public_ip")
    async def query_public_ip(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[str | MessageEventResult, None]:
        """查询运行机器人这台服务器的公网 IP、外网地址或出口 IP。

        用户只说“IP”“IP查询”“查IP”“服务器IP”“公网IP多少”“IP变了吗”时调用。
        返回的是机器人服务器的出口地址，不是聊天用户手机/电脑的 IP。
        用户给出具体 IP 问归属地时不要调用本工具，可使用 IP 定位工具。

        Yields:
            The lookup result through AstrBot's tool result channel.
        """
        if not self.ip_enabled:
            yield "公网 IP 工具已关闭。"
            return
        errors = []
        async with httpx.AsyncClient(
            timeout=self.ip_timeout, trust_env=False
        ) as client:
            for url in IP_ENDPOINTS:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    address = ipaddress.ip_address(response.text.strip())
                    yield f"当前服务器公网 IP：{address}"
                    return
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(type(exc).__name__)
        yield f"公网 IP 查询失败：{', '.join(errors)}"

    @filter.llm_tool(name="query_server_status")
    async def query_server_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查询本服务器实时状态并直接发送完整状态图片，保留原版 PicStatus 风格。

        用户说“状态”“服务器状态”“系统状态”“CPU/内存/磁盘/显存用了多少”
        “机器卡不卡”“开机多久”“进程排行”时调用；单独的“状态”在此机器人语境下指宿主机状态。
        工具直接发图，不需要转交绘图代理、再次复述全量数据或重复发送图片。
        查询的是运行机器人的宿主机，不是游戏服务器在线人数或别人的服务器。

        Yields:
            A status image delivered directly without a second LLM summary.
        """
        if not self.status_enabled:
            yield event.plain_result("服务器状态工具已关闭。")
            return
        if self.only_admin and not event.is_admin():
            yield event.plain_result("当前配置仅允许管理员查询服务器状态。")
            return
        data: dict[str, Any] | None = None
        output: Path | None = None
        rendered = False
        try:
            # Bound concurrent collection/render work and use one output per invocation.
            async with self._status_lock:
                data = await self.build_status_data(event)
                from .status_renderer import StatusRenderer

                if self._renderer is None:
                    self._renderer = StatusRenderer()
                directory = Path(get_astrbot_temp_path()) / "sys_tools"
                directory.mkdir(parents=True, exist_ok=True)
                suffix = "jpg" if self.image_format == "jpeg" else "png"
                output = directory / f"status-{uuid.uuid4().hex}.{suffix}"
                event.track_temporary_local_file(str(output))
                await self._renderer.render(data, str(output))
                rendered = True
                logger.info("[sys_tools] Status card rendered: %s", output.name)
            # AstrBot's local tool executor sends MessageEventResult before closing the loop.
            yield event.image_result(str(output))
            await self.observe_reply(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[sys_tools] Status card failed: %s", type(exc).__name__)
            message = (
                "状态图生成失败，以下为实时采集的文字结果：\n"
                + format_status_text(data)
                if data
                else f"服务器状态采集失败：{type(exc).__name__}"
            )
            yield event.plain_result(message)
        finally:
            # Successful files are owned by the event cleanup, never shared with another user.
            if output and output.exists() and not rendered:
                output.unlink(missing_ok=True)

    async def terminate(self) -> None:
        """Release Chromium on plugin disable or hot reload."""
        if self._renderer is not None:
            await self._renderer.close()
            self._renderer = None
