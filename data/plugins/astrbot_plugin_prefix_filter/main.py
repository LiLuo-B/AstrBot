from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger

@register("prefix_filter", "AIGuard", "一个用于过滤特定前缀消息的插件", "0.1.0")
class PrefixFilter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 获取忽略前缀列表
        self.ignored_prefixes = self.config.get("ignored_prefixes")
        logger.info(f"已加载忽略前缀列表: {self.ignored_prefixes}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def filter_prefix_messages(self, event: AstrMessageEvent):
        '''过滤指定前缀的消息'''
        # 获取消息的纯文本内容
        message_text = event.message_str

        # 检查消息是否以忽略前缀开头
        if any(message_text.startswith(prefix) for prefix in self.ignored_prefixes):
            logger.info(f"忽略前缀消息: {message_text}")
            # 停止事件传播，不进行任何回复
            return event.stop_event()

        # 如果消息不以忽略前缀开头，正常处理
        return None

    @filter.command("prefix_list")
    async def show_prefix_list(self, event: AstrMessageEvent):
        '''显示当前忽略的前缀列表'''
        if not self.ignored_prefixes:
            yield event.plain_result("当前没有设置忽略的前缀")
        else:
            prefix_list = "\n".join([f"- {prefix}" for prefix in self.ignored_prefixes])
            yield event.plain_result(f"当前忽略的前缀列表:\n{prefix_list}")

    @filter.command("prefix_add")
    async def add_prefix(self, event: AstrMessageEvent):
        '''添加需要忽略的前缀'''
        message_text = event.message_str
        # 移除命令本身，获取参数
        parts = message_text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请提供要添加的前缀")
            return

        new_prefix = parts[1].strip()
        if new_prefix in self.ignored_prefixes:
            yield event.plain_result(f"前缀 '{new_prefix}' 已在列表中")
            return

        self.ignored_prefixes.append(new_prefix)
        self.config.save_config()
        yield event.plain_result(f"已添加前缀 '{new_prefix}' 到忽略列表")
        logger.info(f"已添加前缀 '{new_prefix}' 到忽略列表")

    @filter.command("prefix_remove")
    async def remove_prefix(self, event: AstrMessageEvent):
        '''移除忽略的前缀'''
        message_text = event.message_str
        # 移除命令本身，获取参数
        parts = message_text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请提供要移除的前缀")
            return

        prefix_to_remove = parts[1].strip()
        if prefix_to_remove not in self.ignored_prefixes:
            yield event.plain_result(f"前缀 '{prefix_to_remove}' 不在忽略列表中")
            return

        self.ignored_prefixes.remove(prefix_to_remove)
        self.config.save_config()
        yield event.plain_result(f"已从忽略列表中移除前缀 '{prefix_to_remove}'")
        logger.info(f"已从忽略列表中移除前缀 '{prefix_to_remove}'")
