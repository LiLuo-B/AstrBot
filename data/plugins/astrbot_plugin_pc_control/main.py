from collections.abc import AsyncGenerator

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from .wol import WakeOnLAN


@register(
    "pc_control",
    "Li Luo-B",
    "用于控制局域网内的PC，支持唤醒功能",
    "v0.1.0",
)
class PCControlPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pc = self.config.get("pc", {})
        self.mac = self.pc.get("mac", "")
        self.ip = self.pc.get("ip", "")
        self.port = self.pc.get("port", 9)

    @filter.llm_tool(name="wake_pc")
    async def wake_pc(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """唤醒电脑开机。
        当用户想要开启电脑、唤醒电脑、打开电脑时使用此工具。
        """
        if not event.is_admin():
            yield event.plain_result("⚠️ 只有管理员才能执行此操作")
            return
        if not self.mac:
            yield event.plain_result("电脑未配置MAC地址，请先在插件配置中设置")
            return
        message = await WakeOnLAN().wake(self.mac, self.ip, self.port)
        yield event.plain_result(message)
