# main.py
import re
import math
import random
import asyncio
from collections import defaultdict, deque
from typing import List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record
from astrbot.core.star.session_llm_manager import SessionServiceManager


class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # --- 1. 配置兼容性与迁移逻辑 ---
        self._migrate_config()

        # 智能回复：按会话缓存消息 ID，供发送前判断“是否被新消息插嘴”
        self._message_queues = defaultdict(deque)
        self._last_smart_reply_mark = {}

        # 定义成对出现的字符，在智能分段时避免在这些符号内部切断
        self.pair_map = {
            '“': '”', "《": "》", "（": "）", "(": ")",
            "[": "]", "{": "}", "‘": "’", "【": "】", "<": ">",
        }
        # 定义引用/引号字符
        self.quote_chars = {'"', "'", "`"}
        self.secondary_pattern = re.compile(r"[，,、；;]+")

    def _get_cfg(self, key: str, default: Any = None) -> Any:
        """
        助手函数：自动从嵌套或扁平结构中获取配置项。
        解决嵌套配置后代码无法读取旧配置或默认值的问题。
        """
        # 定义分类映射
        categories = [
            "basic_settings", "split_settings", "clean_settings", 
            "reply_media_settings", "delay_settings"
        ]
        # 1. 尝试从嵌套结构获取
        for cat in categories:
            cat_obj = self.config.get(cat)
            if isinstance(cat_obj, dict) and key in cat_obj:
                return cat_obj[key]
        
        # 2. 尝试从顶层获取（兼容旧配置或未迁移的情况）
        return self.config.get(key, default)

    def _migrate_config(self):
        """
        处理旧版本配置数据类型冲突及嵌套迁移。
        防止用户升级插件后配置“丢失”。
        """
        # 1. 键名迁移: clean_items -> clean_before_items
        if "clean_items" in self.config and "clean_before_items" not in self.config:
            logger.info("[Splitter] 迁移旧配置项 clean_items 至 clean_before_items")
            self.config["clean_before_items"] = self.config.pop("clean_items")

        # 2. 结构迁移：将顶层的扁平配置移动到嵌套对象中
        mapping = {
            "basic_settings": ["enable_group_split", "split_scope", "max_length_no_split", "max_length_to_disable", "conversation_blacklist", "conversation_whitelist"],
            "split_settings": ["split_mode", "split_chars", "split_regex", "enable_smart_split", "balanced_split_mode", "max_segments", "min_segment_length", "balanced_split_ratio_min", "balanced_split_ratio_max", "trim_segment_edge_blank_lines"],
            "clean_settings": ["clean_before_items", "clean_after_items", "clean_before_regex", "clean_after_regex", "inject_kaomoji_prompt"],
            "reply_media_settings": ["enable_smart_reply", "enable_reply", "image_strategy", "at_strategy", "face_strategy", "other_media_strategy"],
            "delay_settings": ["delay_strategy", "linear_base", "linear_factor", "log_base", "log_factor", "random_min", "random_max", "fixed_delay"]
        }

        for cat, keys in mapping.items():
            if cat not in self.config or not isinstance(self.config[cat], dict):
                self.config[cat] = {}
            for key in keys:
                if key in self.config and key != cat:
                    val = self.config.pop(key)
                    # 强制类型转换，防止列表配置项变成字符串
                    list_fields = ["split_chars", "clean_before_items", "clean_after_items", "conversation_blacklist", "conversation_whitelist"]
                    if key in list_fields:
                        if isinstance(val, str):
                            val = [val] if key != "split_chars" else list(val)
                        elif isinstance(val, list):
                            val = [str(i) for i in val if i is not None]
                    self.config[cat][key] = val

    def _get_conversation_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _get_message_queue(self, event: AstrMessageEvent):
        return self._message_queues[self._get_conversation_key(event)]

    def _remember_incoming_message(self, event: AstrMessageEvent) -> None:
        message_id = getattr(event.message_obj, "message_id", None)
        if not message_id: return
        queue = self._get_message_queue(event)
        queue.append(str(message_id))
        if len(queue) > 200: queue.popleft()

    def _mark_bot_reply(self, event: AstrMessageEvent, base_message_id: str) -> None:
        if not base_message_id: return
        conv_key = self._get_conversation_key(event)
        mark = "__bot_reply__{}".format(base_message_id)
        queue = self._message_queues[conv_key]
        if self._last_smart_reply_mark.get(conv_key) != mark:
            queue.append(mark)
            self._last_smart_reply_mark[conv_key] = mark
            if len(queue) > 200: queue.popleft()

    def _should_add_smart_reply(self, event: AstrMessageEvent) -> bool:
        if not self._get_cfg("enable_smart_reply", False): return False
        platform_name = str(getattr(event, "get_platform_name", lambda: "")() or "")
        if platform_name.lower() == "dingtalk": return False
        message_id = getattr(event.message_obj, "message_id", None)
        if not message_id: return False
        queue = self._get_message_queue(event)
        queue_str = [str(x) for x in queue]
        msg_id = str(message_id)
        if msg_id not in queue_str: return False
        idx = queue_str.index(msg_id)
        pushed = len(queue_str) - idx - 1
        return pushed > 0

    def _has_reply_component(self, chain: List[BaseMessageComponent]) -> bool:
        return any(isinstance(c, Reply) for c in chain)

    def _prepend_reply(self, chain: List[BaseMessageComponent], message_id: str) -> None:
        if message_id and not self._has_reply_component(chain):
            chain.insert(0, Reply(id=message_id))

    def _remove_reply_components(self, chain: List[BaseMessageComponent]) -> List[BaseMessageComponent]:
        return [comp for comp in chain if not isinstance(comp, Reply)]

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        self_id_getter = getattr(event, "get_self_id", None)
        sender_id_getter = getattr(event, "get_sender_id", None)
        try:
            self_id = self_id_getter() if callable(self_id_getter) else None
            sender_id = sender_id_getter() if callable(sender_id_getter) else None
        except:
            self_id, sender_id = None, None
        if self_id is not None and sender_id is not None and str(sender_id) == str(self_id):
            return
        self._remember_incoming_message(event)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._get_cfg("inject_kaomoji_prompt", True): return
        instruction = (
            "\n【特别注意】如果你需要输出颜文字（如 (QAQ)），请务必使用三对反引号包裹，"
            "格式如：```(QAQ)```。这能确保颜文字作为一个整体被发送，不会被分段工具切断。"
        )
        req.system_prompt += instruction

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        setattr(event, "__is_llm_reply", True)

    def _is_model_generated_reply(self, event: AstrMessageEvent, result) -> bool:
        if not result: return False
        is_model_result = getattr(result, "is_model_result", None)
        if callable(is_model_result):
            try: return bool(is_model_result())
            except: pass
        content_type = getattr(result, "result_content_type", None)
        if content_type is not None:
            type_name = getattr(content_type, "name", "")
            return type_name in {"LLM_RESULT", "AGENT_RUNNER_ERROR", "AGENT_RUNNER_RESULT", "TOOL_RESULT", "TOOL_CALL"}
        return getattr(event, "__is_llm_reply", False)

    @filter.on_decorating_result(priority=-100000000000000000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain: return
        if getattr(result, "__splitter_processed", False): return

        # --- 1. 基础校验 ---
        umo = event.unified_msg_origin
        blacklist = self._get_cfg("conversation_blacklist", [])
        whitelist = self._get_cfg("conversation_whitelist", [])
        if umo in blacklist: return
        if whitelist and umo not in whitelist: return
        if not self._get_cfg("enable_group_split", True) and event.message_obj.group_id: return

        split_scope = self._get_cfg("split_scope", "llm_only")
        is_llm_reply = self._is_model_generated_reply(event, result)
        if split_scope == "llm_only" and not is_llm_reply: return

        # --- 2. 长度校验 ---
        total_text_len = sum(len(c.text) for c in result.chain if isinstance(c, Plain))
        max_len_no_split = self._get_cfg("max_length_no_split", 0)
        if max_len_no_split > 0 and total_text_len < max_len_no_split: return
        max_len_disable = self._get_cfg("max_length_to_disable", 0)
        if max_len_disable > 0 and total_text_len > max_len_disable: return

        setattr(result, "__splitter_processed", True)
        split_mode = self._get_cfg("split_mode", "regex")

        # --- 3. 分段前清理 ---
        if split_mode == "simple":
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    for item in self._get_cfg("clean_before_items", []):
                        if item: comp.text = comp.text.replace(item, "")
        else:
            regex = self._get_cfg("clean_before_regex", "")
            if regex:
                for comp in result.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = re.sub(regex, "", comp.text, flags=re.DOTALL)

        # 脱敏处理
        has_external_at = False
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                if "\u200b" in comp.text: has_external_at = True
                comp.text = comp.text.replace("\u200b \u200b", "__ZWSP_DOUBLE__").replace("\u200b", "__ZWSP_SINGLE__")

        # --- 4. 构建正则 ---
        if split_mode == "simple":
            chars = self._get_cfg("split_chars", ["。", "？", "！", "?", "!", "；", ";", "\n"])
            processed = []
            for c in chars:
                if not c: continue
                processed.append(re.escape(str(c).replace("\\n", "\n").replace("\\t", "\t")))
            processed.sort(key=len, reverse=True)
            split_pattern = "(?:{})+".format("|".join(processed)) if processed else r"[\n]+"
        else:
            split_pattern = self._get_cfg("split_regex", r"[。？！?!\\n…]+")

        # --- 5. 执行切分 ---
        strategies = {
            "image": self._get_cfg("image_strategy", "单独"),
            "at": self._get_cfg("at_strategy", "跟随下段"),
            "face": self._get_cfg("face_strategy", "嵌入"),
            "default": self._get_cfg("other_media_strategy", "跟随下段"),
        }
        
        max_segs = self._get_cfg("max_segments", 7)
        ideal_length = 0
        if self._get_cfg("balanced_split_mode", False) and max_segs > 0:
            text_weight = sum(len(c.text.replace(" ", "")) for c in result.chain if isinstance(c, Plain))
            solo_count = sum(1 for c in result.chain if not isinstance(c, (Plain, Reply)) and strategies.get(type(c).__name__.lower(), "default") == "单独")
            target_segs = max(1, max_segs - solo_count)
            if text_weight > 0:
                ideal_length = max(math.ceil(text_weight / target_segs), self._get_cfg("min_segment_length", 10))

        segments = self.split_chain_smart(result.chain, split_pattern, self._get_cfg("enable_smart_split", True), strategies, self._get_cfg("enable_reply", True), ideal_length)

        # 强制分段上限控制
        if max_segs > 0 and len(segments) > max_segs:
            merged_last = []
            for seg in segments[max_segs - 1:]:
                merged_last.extend(seg)
                
            optimized_last = []
            # 合并连贯的 Plain 组件避免由于强制合并导致文本内部被打断
            for comp in merged_last:
                if optimized_last and isinstance(comp, Plain) and isinstance(optimized_last[-1], Plain):
                    optimized_last[-1] = Plain(optimized_last[-1].text + comp.text)
                else:
                    optimized_last.append(comp)
                    
            segments = segments[:max_segs - 1] + [optimized_last]

        # 均分模式尾部合并
        if self._get_cfg("balanced_split_mode", False) and len(segments) >= 2:
            last_text = "".join([c.text for c in segments[-1] if isinstance(c, Plain)]).strip()
            if 0 < len(last_text) < self._get_cfg("min_segment_length", 10):
                if not any(not isinstance(c, (Plain, Reply)) for c in segments[-1]):
                    segments[-2].extend(segments.pop())

        # --- 6. 回复处理 ---
        source_id = str(getattr(event.message_obj, "message_id", "") or "")
        enable_reply = self._get_cfg("enable_reply", True)
        enable_smart = self._get_cfg("enable_smart_reply", False)

        if segments and source_id:
            if enable_smart:
                if self._should_add_smart_reply(event): self._prepend_reply(segments[0], source_id)
            elif enable_reply:
                self._prepend_reply(segments[0], source_id)

        # --- 7. 后处理 (At/清理/TTS) ---
        at_strategy = strategies.get("at", "跟随下段")
        at_needs_proc = at_strategy in ["接下文", "跟随下段", "嵌入"] and any(type(c).__name__.lower() == "at" for c in result.chain)
        
        for seg in segments:
            if self._get_cfg("trim_segment_edge_blank_lines", True): self._trim_segment_edge_blank_lines(seg)
            for comp in seg:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = comp.text.replace("__ZWSP_DOUBLE__", "\u200b \u200b").replace("__ZWSP_SINGLE__", "\u200b")
                    # 后置清理
                    if split_mode == "simple":
                        for item in self._get_cfg("clean_after_items", []):
                            if item: comp.text = comp.text.replace(item, "")
                    else:
                        regex = self._get_cfg("clean_after_regex", "")
                        if regex: comp.text = re.sub(regex, "", comp.text, flags=re.DOTALL)

        if len(segments) <= 1 and not at_needs_proc:
            final = segments[0] if segments else []
            if enable_smart and not enable_reply: final = self._remove_reply_components(final)
            result.chain.clear(); result.chain.extend(final); return

        # --- 8. 发送 ---
        for i in range(len(segments) - 1):
            seg_chain = segments[i]
            if i > 0 and enable_smart and not enable_reply: seg_chain = self._remove_reply_components(seg_chain)
            text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
            if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain): continue
            
            try:
                seg_chain = await self._process_tts_for_segment(event, seg_chain)
                self._log_segment(i + 1, len(segments), seg_chain, "主动发送")
                mc = MessageChain(); mc.chain = seg_chain
                await self.context.send_message(event.unified_msg_origin, mc)
                await asyncio.sleep(self.calculate_delay(text_content))
            except Exception as e:
                logger.error(f"[Splitter] 发送失败: {e}")

        if enable_smart and source_id: self._mark_bot_reply(event, source_id)

        last_seg = segments[-1]
        if enable_smart and not enable_reply: last_seg = self._remove_reply_components(last_seg)
        result.chain.clear(); result.chain.extend(last_seg)

    def _log_segment(self, index: int, total: int, chain: List[BaseMessageComponent], method: str):
        content = "".join([c.text if isinstance(c, Plain) else f"[{type(c).__name__}]" for c in chain])
        logger.info("[Splitter] 第 {}/{} 段 ({}): {}".format(index, total, method, content.replace('\n', '\\n')))

    def _trim_segment_edge_blank_lines(self, segment: List[BaseMessageComponent]) -> None:
        f_p = next((c for c in segment if isinstance(c, Plain)), None)
        l_p = next((c for c in reversed(segment) if isinstance(c, Plain)), None)
        if f_p and f_p.text: f_p.text = re.sub(r'^(?:[ \t]*\r?\n)+', '', f_p.text)
        if l_p and l_p.text: l_p.text = re.sub(r'(?:\r?\n[ \t]*)+$', '', l_p.text)

    async def _process_tts_for_segment(self, event: AstrMessageEvent, segment: List[BaseMessageComponent]) -> List[BaseMessageComponent]:
        if not self._get_cfg("enable_tts_for_segments", True): return segment
        try:
            all_cfg = self.context.get_config(event.unified_msg_origin)
            tts_cfg = all_cfg.get("provider_tts_settings", {})
            if not tts_cfg.get("enable", False): return segment
            tts_prov = self.context.get_using_tts_provider(event.unified_msg_origin)
            if not tts_prov or not await SessionServiceManager.should_process_tts_request(event): return segment
            if random.random() > float(tts_cfg.get("trigger_probability", 1.0)): return segment
            dual = tts_cfg.get("dual_output", False)
            new_seg = []
            for comp in segment:
                if isinstance(comp, Plain) and len(comp.text) > 1:
                    try:
                        path = await tts_prov.get_audio(comp.text)
                        if path:
                            new_seg.append(Record(file=path, url=path))
                            if dual: new_seg.append(comp)
                        else: new_seg.append(comp)
                    except: new_seg.append(comp)
                else: new_seg.append(comp)
            return new_seg
        except: return segment

    def calculate_delay(self, text: str) -> float:
        strategy = self._get_cfg("delay_strategy", "linear")
        if strategy == "random": return random.uniform(self._get_cfg("random_min", 1.0), self._get_cfg("random_max", 3.0))
        if strategy == "log": return min(self._get_cfg("log_base", 0.5) + self._get_cfg("log_factor", 0.8) * math.log(len(text) + 1), 5.0)
        if strategy == "linear": return self._get_cfg("linear_base", 0.5) + (len(text) * self._get_cfg("linear_factor", 0.1))
        return self._get_cfg("fixed_delay", 1.5)

    def split_chain_smart(self, chain: List[BaseMessageComponent], pattern: str, smart: bool, strategies: Dict[str, str], enable_reply: bool, ideal: int = 0) -> List[List[BaseMessageComponent]]:
        segments = []; buffer = []; weight = 0
        for comp in chain:
            if isinstance(comp, Plain):
                if not comp.text: continue
                if not smart: self._process_text_simple(comp.text, pattern, segments, buffer); weight = 0
                else: weight = self._process_text_smart(comp.text, pattern, segments, buffer, weight, ideal)
            else:
                c_type = type(comp).__name__.lower()
                if "reply" in c_type:
                    if enable_reply or self._get_cfg("enable_smart_reply", False): buffer.append(comp)
                    continue
                strategy = strategies.get(c_type, strategies.get("default", "跟随下段"))
                if strategy == "单独":
                    if buffer: segments.append(buffer[:]); buffer.clear()
                    segments.append([comp]); weight = 0
                elif strategy == "跟随上段":
                    if buffer: buffer.append(comp); segments.append(buffer[:]); buffer.clear(); weight = 0
                    elif segments: segments[-1].append(comp)
                    else: segments.append([comp])
                elif strategy in ["跟随下段", "接下文"]:
                    if buffer: segments.append(buffer[:]); buffer.clear(); weight = 0
                    buffer.append(comp)
                else: buffer.append(comp)
        if buffer: segments.append(buffer)
        return [s for s in segments if s]

    def _process_text_simple(self, text: str, pattern: str, segments: list, buffer: list):
        parts = re.split("({})".format(pattern), text)
        tmp = ""
        for p in parts:
            if not p: continue
            if re.fullmatch(pattern, p):
                tmp += p; buffer.append(Plain(tmp))
                segments.append(buffer[:]); buffer.clear(); tmp = ""
            else: tmp += p
        if tmp: buffer.append(Plain(tmp))

    def _process_text_smart(self, text: str, pattern: str, segments: list, buffer: list, start_w: int = 0, ideal: int = 0) -> int:
        stack = []; compiled = re.compile(pattern); i = 0; n = len(text); chunk = ""; weight = start_w
        ratio_min = self._get_cfg("balanced_split_ratio_min", 0.4)
        ratio_max = self._get_cfg("balanced_split_ratio_max", 0.9)
        
        while i < n:
            if text.startswith("```", i) and (i == 0 or text[i-1] == chr(10)):
                idx = text.find("```", i + 3)
                if idx != -1: chunk += text[i:idx+3]; weight += idx+3-i; i = idx+3; continue
                else: chunk += text[i:]; weight += n-i; break
            if text.startswith("<think>", i):
                idx = text.find("</think>", i + 7)
                if idx != -1: chunk += text[i:idx+8]; weight += idx+8-i; i = idx+8; continue
                else: chunk += text[i:]; weight += n-i; break

            match = compiled.match(text, pos=i)
            if match:
                delim = match.group(); should = False
                if not stack or "\n" in delim:
                    should = True
                    if ideal > 0 and weight < ideal * ratio_min: should = False
                    if should and "\n" not in delim and re.match(r"^[ \t.?!,;:\-']+$", delim):
                        p_c = text[i-1] if i > 0 else ""; n_c = text[i+len(delim)] if i+len(delim) < n else ""
                        if re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", p_c) and re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", n_c): should = False
                if should:
                    chunk += delim; buffer.append(Plain(chunk))
                    segments.append(buffer[:]); buffer.clear(); chunk = ""; weight = 0; i += len(delim)
                else: chunk += delim; weight += len(delim); i += len(delim)
                continue

            if ideal > 0 and weight >= ideal * ratio_max and not stack:
                sec = self.secondary_pattern.match(text, pos=i)
                if sec:
                    delim = sec.group()
                    chunk += delim; buffer.append(Plain(chunk))
                    segments.append(buffer[:]); buffer.clear(); chunk = ""; weight = 0; i += len(delim)
                    continue

            char = text[i]
            if char in self.quote_chars:
                if stack and stack[-1] == char: stack.pop()
                else: stack.append(char)
            elif not stack and char in self.pair_map: stack.append(char)
            elif stack and char == self.pair_map.get(stack[-1]): stack.pop()
            
            chunk += char; i += 1; weight += 1 if not char.isspace() else 0
        if chunk: buffer.append(Plain(chunk))
        return weight
