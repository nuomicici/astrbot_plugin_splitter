import re
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain, BaseMessageComponent

@register("astrbot_plugin_splitter", "YourName", "LLM 输出自动分段发送插件", "1.0.2")
class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # 1. 标记阶段：当 LLM 生成响应时，给 Event 打上标记
    # 参考: listen-message-event.md -> 事件钩子 -> LLM 请求完成时
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 动态给 event 对象挂载一个属性，用于在后续流程识别
        setattr(event, "__is_llm_reply", True)

    # 2. 拦截与分发阶段：发送消息前触发
    # 参考: listen-message-event.md -> 事件钩子 -> 发送消息前
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 检查标记：如果不是 LLM 的回复，直接放行
        if not getattr(event, "__is_llm_reply", False):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 获取配置
        pattern = self.config.get("split_regex", r"[。？！\n]+")
        delay = self.config.get("delay", 1.5)

        # 执行分段
        segments = self.split_chain_keep_delimiter(result.chain, pattern)

        # 如果没有触发分段（只有1段），则不做任何处理，让 AstrBot 原样发送
        if len(segments) <= 1:
            return

        logger.info(f"[Splitter] LLM 回复已分段，共 {len(segments)} 段。")

        # 遍历分段并发送
        for i, segment_chain in enumerate(segments):
            if not segment_chain:
                continue

            try:
                # 构造消息链对象
                mc = MessageChain()
                mc.chain = segment_chain
                
                # 发送消息
                # 参考: send-message.md -> 主动消息
                await self.context.send_message(event.unified_msg_origin, mc)

                # 如果不是最后一段，等待
                if i < len(segments) - 1:
                    await asyncio.sleep(delay)

            except Exception as e:
                logger.error(f"[Splitter] 发送分段 {i+1} 失败: {e}")

        # 3. 关键处理：清空原始消息链
        # 我们不使用 stop_event()，因为那会中断 AstrBot 对用户消息的记录流程。
        # 我们只是把“原本要发的一大坨消息”清空，让 AstrBot 认为“发送了一个空消息”，
        # 这样既不会重复发送，也能保证会话生命周期正常结束（用户消息会被正常记录）。
        result.chain.clear()

    def split_chain_keep_delimiter(self, chain: List[BaseMessageComponent], pattern: str) -> List[List[BaseMessageComponent]]:
        """
        分段算法：保留标点符号，将其附着在分割后的文本末尾。
        """
        segments = []
        current_chain_buffer = []

        for component in chain:
            if isinstance(component, Plain):
                text = component.text
                
                # 使用捕获组 () 包裹正则，re.split 会保留分隔符
                # 例如 pattern = [!\n], text = "Hi! Hi" -> ['Hi', '!', ' Hi']
                parts = re.split(f"({pattern})", text)
                
                # 遍历处理切割结果
                # parts 的结构通常是: [文本, 分隔符, 文本, 分隔符, ...]
                i = 0
                while i < len(parts):
                    part_text = parts[i]
                    
                    if not part_text: # 空字符串跳过（可能是正则在开头或结尾匹配产生的）
                        i += 1
                        continue
                    
                    # 检查下一个元素是否是分隔符 (因为使用了捕获组，分隔符在奇数索引位)
                    # 但在这里我们的 parts 列表里，分隔符就在 i+1 的位置（如果存在）
                    # 修正逻辑：re.split 输出里，偶数位是内容，奇数位是捕获的分隔符
                    
                    # 当前部分加入 buffer
                    current_chain_buffer.append(Plain(part_text))
                    
                    # 检查是否有紧跟的分隔符
                    if i + 1 < len(parts):
                        delimiter = parts[i+1]
                        # 将分隔符也加入当前 buffer
                        current_chain_buffer.append(Plain(delimiter))
                        
                        # 遇到分隔符，说明这一段结束了
                        segments.append(current_chain_buffer)
                        current_chain_buffer = [] # 重置 buffer
                        
                        i += 2 # 跳过文本和分隔符
                    else:
                        # 没有分隔符了，说明是最后一段文本，留在 buffer 里等待后续组件
                        i += 1
            else:
                # 非文本组件（图片、At等）直接加入当前 buffer
                current_chain_buffer.append(component)

        # 处理剩余的 buffer
        if current_chain_buffer:
            segments.append(current_chain_buffer)

        # 过滤掉可能的空分段
        return [seg for seg in segments if seg]
