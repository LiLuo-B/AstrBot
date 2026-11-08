import socket


class WakeOnLAN:
    def _create_magic_packet(self, mac_address: str) -> bytes:
        mac = mac_address.replace(":", "").replace("-", "").replace(".", "")
        if len(mac) != 12:
            raise ValueError(f"Invalid MAC address format: {mac_address}")
        mac_bytes = bytes.fromhex(mac)
        return b"\xff" * 6 + mac_bytes * 16

    async def wake(self, mac_address: str, ip_address: str, port: int) -> str:
        try:
            magic_packet = self._create_magic_packet(mac_address)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet, (ip_address, port))
            sock.close()

            return f"魔术包已发送到 {mac_address}"
        except Exception as e:
            return f"唤醒失败: {str(e)}"
