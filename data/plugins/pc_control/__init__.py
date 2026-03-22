"""
PC Control Plugin - Control PC power via LAN

A plugin for AstrBot to control PCs in LAN (wake up / shutdown).
"""

import asyncio
import socket
import struct
from typing import Optional

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter


def create_magic_packet(mac_address: str) -> bytes:
    """Create a Wake-on-LAN magic packet."""
    mac = mac_address.replace(":", "").replace("-", "").replace(".", "")
    if len(mac) != 12:
        raise ValueError("Invalid MAC address format")
    mac_bytes = bytes.fromhex(mac)
    return b"\xff" * 6 + mac_bytes * 16


@star.register(
    "pc_control",
    "Your Name",
    "Control PC power (wake on LAN / shutdown via SSH)",
    "1.0.0",
)
class PCControl(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.config = context.get_config().get("pc_control", {})

    async def terminate(self) -> None:
        pass

    @filter.command("wake")
    async def wake_pc(
        self, event: AstrMessageEvent, mac: str = "", broadcast: str = ""
    ) -> None:
        """Wake up a PC via Wake-on-LAN.

        Usage: /wake <MAC地址> [广播地址]
        Example: /wake AA:BB:CC:DD:EE:FF
        Example: /wake AA:BB:CC:DD:EE:FF 192.168.1.255
        """
        if not mac:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /wake <MAC地址> [广播地址]\n"
                    "示例: /wake AA:BB:CC:DD:EE:FF\n"
                    "示例: /wake AA:BB:CC:DD:EE:FF 192.168.1.255"
                )
            )
            return

        try:
            magic_packet = create_magic_packet(mac)
            target_ip = broadcast if broadcast else "255.255.255.255"
            target_port = 9

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet, (target_ip, target_port))
            sock.close()

            event.set_result(
                MessageEventResult().message(f"已发送 Wake-on-LAN 包到 {mac}")
            )
        except Exception as e:
            event.set_result(MessageEventResult().message(f"发送失败: {str(e)}"))

    @filter.command("shutdown")
    async def shutdown_pc(
        self,
        event: AstrMessageEvent,
        ip: str = "",
        port: str = "22",
        username: str = "",
        password: str = "",
    ) -> None:
        """Shutdown a PC via SSH.

        Usage: /shutdown <IP> <端口> <用户名> <密码>
        Example: /shutdown 192.168.1.100 22 root password
        """
        if not ip or not username or not password:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /shutdown <IP> <端口> <用户名> <密码>\n"
                    "示例: /shutdown 192.168.1.100 22 root yourpassword\n\n"
                    "⚠️ 注意: 目标PC需开启SSH服务且有sudo权限"
                )
            )
            return

        try:
            port = int(port)
        except ValueError:
            event.set_result(MessageEventResult().message("端口必须是数字"))
            return

        event.set_result(MessageEventResult().message(f"正在通过 SSH 连接到 {ip} ..."))

        result = await self._ssh_shutdown(ip, port, username, password)
        event.set_result(MessageEventResult().message(result))

    async def _ssh_shutdown(
        self, ip: str, port: int, username: str, password: str
    ) -> str:
        """Execute shutdown command via SSH."""
        try:
            import paramiko
        except ImportError:
            return "错误: 需要安装 paramiko 库\n请运行: pip install paramiko"

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(ip, port, username, password, timeout=10)

            stdin, stdout, stderr = client.exec_command(
                "sudo shutdown -h now", get_pty=True
            )
            stdin.write(password + "\n")
            stdin.flush()

            exit_status = stdout.channel.recv_exit_status()
            client.close()

            if exit_status == 0:
                return f"✅ 已发送关机命令到 {ip}"
            else:
                error = stderr.read().decode()
                return f"❌ 关机失败: {error}"
        except Exception as e:
            return f"❌ 连接失败: {str(e)}"

    @filter.command("ping")
    async def ping_pc(self, event: AstrMessageEvent, ip: str = "") -> None:
        """Ping a PC to check if it's online.

        Usage: /ping <IP>
        Example: /ping 192.168.1.100
        """
        if not ip:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /ping <IP>\n示例: /ping 192.168.1.100"
                )
            )
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                "1",
                "-W",
                "2",
                ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                event.set_result(MessageEventResult().message(f"✅ {ip} 在线"))
            else:
                event.set_result(MessageEventResult().message(f"❌ {ip} 离线"))
        except Exception as e:
            event.set_result(MessageEventResult().message(f"错误: {str(e)}"))
