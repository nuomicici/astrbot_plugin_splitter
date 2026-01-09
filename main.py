import re
import math
import random
import asyncio
from typing import List, Dict

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record
from astrbot.core.star.session_llm_manager import SessionServiceManager

class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pair_map = {
            '"': '"', '《': '》', '（': '）', '(': ')', 
            '[': ']', '{': '}', "'": "'", '【': '】', '<': '>'
        }
        self.quote_chars = {'"', "'", "`"}

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        setattr(event, "__is_llm_reply", True)

    @filter.on_decorating_result(priority=-100000000000000000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 1. 基础防重入与校验
        if getattr(event, "__splitter_processed", False):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 2. 作用范围检查
        split_scope = self.config.get("split_scope", "llm_only")
        is_llm_reply = getattr(event, "__is_llm_reply", False)

        if split_scope == "llm_only" and not is_llm_reply:
            return

        # 3. 长度限制检查
        max_len_no_split = self.config.get("max_length_no_split", 0)
        total_text_len = sum(len(c.text) for c in result.chain if isinstance(c, Plain))

        if max_len_no_split > 0 and total_text_len < max_len_no_split:
            return

        # 标记已处理
        setattr(event, "__splitter_processed", True)

        # 4. 获取配置
        split_mode = self.config.get("split_mode", "regex")
        if split_mode == "simple":
            split_chars = self.config.get("split_chars", "。？！?!；;\n")
            split_pattern = f"[{re.escape(split_chars)}]+"
        else:
            split_pattern = self.config.get("split_regex", r"[。？！?!\\n…]+")

        clean_pattern = self.config.get("clean_regex", "")
        smart_mode = self.config.get("enable_smart_split", True)
        max_segs = self.config.get("max_segments", 7)
        enable_reply = self.config.get("enable_reply", True)

        strategies = {
            'image': self.config.get("image_strategy", "单独"),
            'at': self.config.get("at_strategy", "跟随下段"),
            'face': self.config.get("face_strategy", "嵌入"),
            'default': self.config.get("other_media_strategy", "跟随下段")
        }

        # 5. 执行分段
        segments = self.split_chain_smart(result.chain, split_pattern, smart_mode, strategies, enable_reply)

        # 6. 最大分段数限制
        if len(segments) > max_segs and max_segs > 0:
            logger.warning(f"[Splitter] 分段数({len(segments)}) 超过限制({max_segs})，合并剩余段落。")
            merged_last = []
            final_segments = segments[:max_segs-1]
            for seg in segments[max_segs-1:]:
                merged_last.extend(seg)
            final_segments.append(merged_last)
            segments = final_segments

        # 如果只有一段，且不需要清理，直接放行
        if len(segments) <= 1 and not clean_pattern:
            return

        # 7. 注入引用 (Reply) - 仅第一段
        if enable_reply and segments and event.message_obj.message_id:
            has_reply = any(isinstance(c, Reply) for c in segments[0])
            if not has_reply:
                segments[0].insert(0, Reply(id=event.message_obj.message_id))

        logger.info(f"[Splitter] 消息被分为 {len(segments)} 段。")

        # 8. 逐段处理
        
        # 先应用清理正则到所有段落
        if clean_pattern:
            for seg in segments:
                for comp in seg:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = re.sub(clean_pattern, "", comp.text)

        # 发送前 N-1 段
        for i in range(len(segments) - 1):
            segment_chain = segments[i]
            
            # 空内容检查
            text_content = "".join([c.text for c in segment_chain if isinstance(c, Plain)])
            has_media = any(not isinstance(c, Plain) for c in segment_chain)
            if not text_content.strip() and not has_media:
                continue

            try:
                # --- 处理TTS ---
                segment_chain = await self._process_tts_for_segment(event, segment_chain)
                # ---------------
                
                # --- 日志输出 ---
                self._log_segment(i + 1, len(segments), segment_chain, "主动发送")
                # ---------------

                mc = MessageChain()
                mc.chain = segment_chain
                await self.context.send_message(event.unified_msg_origin, mc)

                # 延迟
                wait_time = self.calculate_delay(text_content)
                await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(f"[Splitter] 发送分段 {i+1} 失败: {e}")

        # 9. 处理最后一段
        last_segment = segments[-1]
        
        last_text = "".join([c.text for c in last_segment if isinstance(c, Plain)])
        last_has_media = any(not isinstance(c, Plain) for c in last_segment)
        
        if not last_text.strip() and not last_has_media:
            result.chain.clear() 
        else:
            # --- 日志输出 ---
            self._log_segment(len(segments), len(segments), last_segment, "交给框架")
            # ---------------
            
            result.chain.clear()
            result.chain.extend(last_segment)

    def _log_segment(self, index: int, total: int, chain: List[BaseMessageComponent], method: str):
        """输出单行段落内容日志"""
        content_str = ""
        for comp in chain:
            if isinstance(comp, Plain):
                content_str += comp.text
            else:
                content_str += f"[{type(comp).__name__}]"
        
        # 替换换行符以便在单行日志中显示，如果需要完全原始输出可去掉 replace
        log_content = content_str.replace('\n', '\\n')
        logger.info(f"[Splitter] 第 {index}/{total} 段 ({method}): {log_content}")

    async def _process_tts_for_segment(self, event: AstrMessageEvent, segment: List[BaseMessageComponent]) -> List[BaseMessageComponent]:
        """为分段处理TTS（如果启用）"""
        # 检查是否启用分段TTS
        enable_tts_for_segments = self.config.get("enable_tts_for_segments", True)
        if not enable_tts_for_segments:
            return segment
        
        # 获取框架TTS配置
        try:
            # 使用和框架相同的逻辑检查TTS是否应该启用
            all_config = self.context.get_config(event.unified_msg_origin)
            tts_config = all_config.get("provider_tts_settings", {})
            tts_enabled = tts_config.get("enable", False)
            
            if not tts_enabled:
                return segment
            
            # 获取TTS provider
            tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
            if not tts_provider:
                return segment
            
            # 检查是否应该处理TTS（会话级别和LLM结果检查）
            result = event.get_result()
            if not result or not result.is_llm_result():
                return segment
            
            if not await SessionServiceManager.should_process_tts_request(event):
                return segment
            
            # 检查触发概率
            tts_trigger_probability = tts_config.get("trigger_probability", 1.0)
            try:
                tts_trigger_probability = max(0.0, min(float(tts_trigger_probability), 1.0))
            except (TypeError, ValueError):
                tts_trigger_probability = 1.0
            
            if random.random() > tts_trigger_probability:
                return segment
            
            # 获取其他TTS配置
            dual_output = tts_config.get("dual_output", False)
            
            # 处理segment中的每个Plain组件
            new_segment = []
            for comp in segment:
                if isinstance(comp, Plain) and len(comp.text) > 1:
                    try:
                        logger.info(f"[Splitter] TTS 请求: {comp.text[:50]}...")
                        audio_path = await tts_provider.get_audio(comp.text)
                        logger.info(f"[Splitter] TTS 结果: {audio_path}")
                        
                        if audio_path:
                            # 创建Record组件
                            new_segment.append(Record(file=audio_path, url=audio_path))
                            # 如果启用双重输出，也添加文本
                            if dual_output:
                                new_segment.append(comp)
                        else:
                            logger.warning(f"[Splitter] TTS 音频文件未找到，使用文本发送")
                            new_segment.append(comp)
                    except Exception as e:
                        logger.error(f"[Splitter] TTS 处理失败: {e}，使用文本发送")
                        new_segment.append(comp)
                else:
                    new_segment.append(comp)
            
            return new_segment
            
        except Exception as e:
            logger.error(f"[Splitter] TTS 配置检查失败: {e}，跳过TTS处理")
            return segment

    def calculate_delay(self, text: str) -> float:
        strategy = self.config.get("delay_strategy", "linear")
        if strategy == "random":
            return random.uniform(self.config.get("random_min", 1.0), self.config.get("random_max", 3.0))
        elif strategy == "log":
            base = self.config.get("log_base", 0.5)
            factor = self.config.get("log_factor", 0.8)
            return min(base + factor * math.log(len(text) + 1), 5.0)
        elif strategy == "linear":
            return self.config.get("linear_base", 0.5) + (len(text) * self.config.get("linear_factor", 0.1))
        else:
            return self.config.get("fixed_delay", 1.5)

    def split_chain_smart(self, chain: List[BaseMessageComponent], pattern: str, smart_mode: bool, strategies: Dict[str, str], enable_reply: bool) -> List[List[BaseMessageComponent]]:
        segments = []
        current_chain_buffer = []

        for component in chain:
            if isinstance(component, Plain):
                text = component.text
                if not text: continue
                if not smart_mode:
                    self._process_text_simple(text, pattern, segments, current_chain_buffer)
                else:
                    self._process_text_smart(text, pattern, segments, current_chain_buffer)
            else:
                c_type = type(component).__name__.lower()
                if 'reply' in c_type:
                    if enable_reply: current_chain_buffer.append(component)
                    continue

                if 'image' in c_type: strategy = strategies['image']
                elif 'at' in c_type: strategy = strategies['at']
                elif 'face' in c_type: strategy = strategies['face']
                else: strategy = strategies['default']

                if strategy == "单独":
                    if current_chain_buffer:
                        segments.append(current_chain_buffer[:])
                        current_chain_buffer.clear()
                    segments.append([component])
                elif strategy == "跟随上段":
                    if current_chain_buffer: current_chain_buffer.append(component)
                    elif segments: segments[-1].append(component)
                    else: current_chain_buffer.append(component)
                else: 
                    current_chain_buffer.append(component)

        if current_chain_buffer:
            segments.append(current_chain_buffer)
        return [seg for seg in segments if seg]

    def _process_text_simple(self, text: str, pattern: str, segments: list, buffer: list):
        parts = re.split(f"({pattern})", text)
        temp_text = ""
        for part in parts:
            if not part: continue
            if re.fullmatch(pattern, part):
                temp_text += part
                buffer.append(Plain(temp_text))
                segments.append(buffer[:])
                buffer.clear()
                temp_text = ""
            else:
                if temp_text: buffer.append(Plain(temp_text)); temp_text = ""
                buffer.append(Plain(part))
        if temp_text: buffer.append(Plain(temp_text))

    def _process_text_smart(self, text: str, pattern: str, segments: list, buffer: list):
        stack = []
        compiled_pattern = re.compile(pattern)
        i = 0
        n = len(text)
        current_chunk = ""

        while i < n:
            char = text[i]
            is_opener = char in self.pair_map
            if char in self.quote_chars:
                if stack and stack[-1] == char: stack.pop()
                else: stack.append(char)
                current_chunk += char; i += 1; continue
            if stack:
                expected_closer = self.pair_map.get(stack[-1])
                if char == expected_closer: stack.pop()
                elif is_opener: stack.append(char)
                current_chunk += char; i += 1; continue
            if is_opener:
                stack.append(char); current_chunk += char; i += 1; continue

            match = compiled_pattern.match(text, pos=i)
            if match:
                delimiter = match.group()
                current_chunk += delimiter
                buffer.append(Plain(current_chunk))
                segments.append(buffer[:])
                buffer.clear()
                current_chunk = ""
                i += len(delimiter)
            else:
                current_chunk += char; i += 1
        if current_chunk: buffer.append(Plain(current_chunk))
