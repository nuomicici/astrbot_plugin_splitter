# 文件名: main.py
import re
import asyncio
from typing import List

# 导入必要的模块
# 参考: listen-message-event.md (事件监听), plugin-config.md (配置), send-message.md (消息组件)
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain, BaseMessageComponent

@register("astrbot_plugin_splitter", "YourName", "LLM 输出自动分段发送插件", "1.0.0")
class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # 参考: listen-message-event.md -> 事件钩子 -> 发送消息前
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        在消息发送前拦截，进行分段处理。
        """
        # 获取即将发送的结果对象
        result = event.get_result()
        if not result or not result.chain:
            return

        # 获取配置
        pattern = self.config.get("split_regex", r"\n\s*\n")
        delay = self.config.get("delay", 1.0)
        
        # 1. 尝试进行分段逻辑
        original_chain = result.chain
        segments = self.split_chain(original_chain, pattern)

        # 2. 如果分段数量大于1，说明触发了分割逻辑
        if len(segments) > 1:
            logger.info(f"[Splitter] 检测到长消息，已分割为 {len(segments)} 段发送。")
            
            # 参考: listen-message-event.md -> 控制事件传播
            # 停止原始事件的传播，防止原始的大段消息被发送出去
            event.stop_event()

            # 参考: send-message.md -> 主动消息
            # 遍历分段并逐一发送
            for i, segment_chain in enumerate(segments):
                if not segment_chain:
                    continue
                
                try:
                    # 【修复点】send_message 需要 MessageChain 对象，而非 list
                    # 实例化 MessageChain 并将列表赋值给 chain 属性
                    mc = MessageChain()
                    mc.chain = segment_chain
                    
                    # 使用 unified_msg_origin 确保发送到正确的会话
                    await self.context.send_message(event.unified_msg_origin, mc)
                    
                    # 避免最后一段发送后还等待
                    if i < len(segments) - 1:
                        await asyncio.sleep(delay)
                except Exception as e:
                    logger.error(f"[Splitter] 发送分段消息失败: {e}")

    def split_chain(self, chain: List[BaseMessageComponent], pattern: str) -> List[List[BaseMessageComponent]]:
        """
        核心分段算法。
        确保非 Plain 组件（如 Image, At）被视为普通文本一样参与流式顺序，
        只有在 Plain 组件内部匹配到正则时才断开。
        """
        segments = []
        current_buffer = []

        for component in chain:
            # 参考: listen-message-event.md -> 消息链 -> Plain
            if isinstance(component, Plain):
                text = component.text
                # 使用正则分割文本
                # re.split 会返回 [text_part1, text_part2, ...]
                parts = re.split(pattern, text)

                if len(parts) == 1:
                    # 没有匹配到分隔符，直接加入当前缓冲区
                    current_buffer.append(component)
                else:
                    # 匹配到了分隔符
                    
                    # 1. 第一部分：归属于“当前段落”
                    if parts[0]:
                        current_buffer.append(Plain(parts[0]))
                    
                    # 此时当前段落结束，推入 segments
                    if current_buffer:
                        segments.append(current_buffer)
                        current_buffer = [] # 重置缓冲区

                    # 2. 中间部分：每一部分都是一个独立的段落
                    # parts[1:-1] 是被分隔符完全包围的段落
                    for mid_part in parts[1:-1]:
                        if mid_part:
                            segments.append([Plain(mid_part)])
                    
                    # 3. 最后一部分：是“下一个段落”的开始
                    if parts[-1]:
                        current_buffer.append(Plain(parts[-1]))
            else:
                # 非文本组件（如图片、At等），直接加入当前缓冲区
                current_buffer.append(component)

        # 循环结束，如果有剩余的 buffer，作为一个段落
        if current_buffer:
            segments.append(current_buffer)

        return segments
