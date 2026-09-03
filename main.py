# main.py
import asyncio
import contextvars
import math
import re
from collections import defaultdict, deque
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import BaseMessageComponent, Plain, Reply, Record
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

try:
    from .splitter.config import (
        apply_advanced_mode_defaults,
        apply_simple_mode_defaults,
        get_adv_cfg,
        get_cfg,
        get_simple_cfg,
        migrate_config,
    )
    from .splitter.proactive import (
        build_synthetic_event,
        build_synthetic_result,
        handle_proactive_send,
        install_send_message_patch,
    )
    from .splitter.reply import (
        get_conversation_key,
        get_message_queue,
        has_reply_component,
        mark_bot_reply,
        prepend_reply,
        remember_incoming_message,
        remove_reply_components,
        should_add_smart_reply,
    )
    from .splitter.splitter import (
        apply_replace_rules,
        calculate_delay,
        split_chain_smart,
        unescape_replace_str,
    )
    from .splitter.tts import (
        chain_has_voice,
        process_tts_for_segment,
        will_use_framework_tts,
    )
except ImportError:
    from splitter.config import (
        apply_advanced_mode_defaults,
        apply_simple_mode_defaults,
        get_adv_cfg,
        get_cfg,
        get_simple_cfg,
        migrate_config,
    )
    from splitter.proactive import (
        build_synthetic_event,
        build_synthetic_result,
        handle_proactive_send,
        install_send_message_patch,
    )
    from splitter.reply import (
        get_conversation_key,
        get_message_queue,
        has_reply_component,
        mark_bot_reply,
        prepend_reply,
        remember_incoming_message,
        remove_reply_components,
        should_add_smart_reply,
    )
    from splitter.splitter import (
        apply_replace_rules,
        calculate_delay,
        split_chain_smart,
        unescape_replace_str,
    )
    from splitter.tts import (
        chain_has_voice,
        process_tts_for_segment,
        will_use_framework_tts,
    )


class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # --- 1. 配置兼容性与迁移逻辑 ---
        migrate_config(self.config)

        # --- 1.5. 模式判定与预设映射 ---
        self._config_mode = self.config.get("config_mode", "简易模式")
        self._is_simple_mode = (self._config_mode == "简易模式")
        self._is_advanced_mode = (self._config_mode == "进阶模式")
        self._is_pro_mode = (self._config_mode == "专业模式")
        self._simple_overrides: dict = {}
        if self._is_simple_mode:
            self._simple_overrides = apply_simple_mode_defaults(self.config)
        elif self._is_advanced_mode:
            self._simple_overrides = apply_advanced_mode_defaults(self.config)

        # 智能回复：按会话缓存消息 ID，供发送前判断"是否被新消息插嘴"
        self._message_queues: defaultdict = defaultdict(deque)
        self._last_smart_reply_mark: dict = {}
        # 防止同一对话并发分段处理导致重复发送
        self._processing_locks: Dict[str, asyncio.Lock] = {}

        # 主动发送拦截：防止拦截器自身调用原始 send_message 时形成无限递归。
        # 使用 ContextVar 做任务级隔离，避免多会话并发互相干扰。
        self._proactive_inhibit = contextvars.ContextVar("__splitter_proactive_inhibit", default=False)

    # ------------------------------------------------------------------
    # 配置读取快捷方法（委托给 splitter.config 模块）
    # ------------------------------------------------------------------
    def _get_cfg(self, key: str, default: Any = None) -> Any:
        return get_cfg(self.config, self._is_pro_mode, self._simple_overrides, key, default)

    def _get_simple_cfg(self, key: str, default: Any = None) -> Any:
        return get_simple_cfg(self.config, key, default)

    def _get_adv_cfg(self, key: str, default: Any = None) -> Any:
        return get_adv_cfg(self.config, key, default)

    def _get_processing_lock(self, conv_key: str) -> asyncio.Lock:
        if conv_key not in self._processing_locks:
            self._processing_locks[conv_key] = asyncio.Lock()
        return self._processing_locks[conv_key]

    def _get_conversation_key(self, event: AstrMessageEvent) -> str:
        return get_conversation_key(event)

    # ------------------------------------------------------------------
    # AstrBot 生命周期钩子
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """AstrBot 初始化完成后，劫持 Context.send_message 以拦截主动发送的消息。

        框架没有为主动发送（self.context.send_message）提供装饰钩子入口，
        主动发送的消息直接走 platform.send_by_session，完全绕过流水线，
        因此 on_decorating_result 无法对其分段。这里通过 monkey-patch
        在 Context.send_message 上包裹一层：将消息链交给分段逻辑处理，
        处理完的多段再依次调用原始 send_message 发出。
        """
        if not self._get_cfg("enable_proactive_split", True):
            logger.info("[Splitter] 主动发送分段已关闭，跳过 send_message 劫持")
            return
        install_send_message_patch(self)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent) -> None:
        self_id_getter = getattr(event, "get_self_id", None)
        sender_id_getter = getattr(event, "get_sender_id", None)
        try:
            self_id = self_id_getter() if callable(self_id_getter) else None
            sender_id = sender_id_getter() if callable(sender_id_getter) else None
        except Exception:
            self_id, sender_id = None, None
        if self_id is not None and sender_id is not None and str(sender_id) == str(self_id):
            return
        remember_incoming_message(self._message_queues, event)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if self._get_cfg("inject_kaomoji_prompt", False):
            instruction = (
                "\n【特别注意】如果你需要输出颜文字（如 (QAQ)），请务必使用三对反引号包裹，"
                "格式如：```(QAQ)```。这能确保颜文字作为一个整体被发送，不会被分段工具切断。"
            )
            req.system_prompt += instruction

        # --- 反向替换：将用户输入中的「替换后文本」还原为「原始文本」再交给 LLM ---
        if not self._get_cfg("reverse_replace", False):
            return
        replace_rules = self._get_cfg("replace_rules", [])
        if not replace_rules:
            return
        reverse_rules = []
        for rule in replace_rules:
            if not isinstance(rule, dict):
                continue
            find = rule.get("find", "")
            replace = rule.get("replace", "")
            if not find or not replace:
                continue
            reverse_rules.append((unescape_replace_str(replace), unescape_replace_str(find)))
        if not reverse_rules:
            return
        if hasattr(req, "prompt") and req.prompt:
            req.prompt = apply_replace_rules(req.prompt, reverse_rules)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        setattr(event, "__is_llm_reply", True)

    @filter.on_decorating_result(priority=-100000000000000000)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.chain:
            return
        # 仅使用 result 级别的锁防止同一 result 对象被重复处理。
        # 注意：不能使用 event 级别的锁，因为 Agent 工具调用场景下
        # 框架会在同一个 event 对象上多次 yield 新的 result（每次工具调用
        # 结束后以及最终回复各产生一个全新的 MessageEventResult 实例），
        # event 级别的锁会导致工具调用后的最终 LLM 回复不被分段。
        # 参见：https://github.com/nuomicici/astrbot_plugin_splitter/issues/32
        if getattr(result, "__splitter_processed", False):
            return

        # --- 1. 基础校验 ---
        if self._is_simple_mode and not self._get_simple_cfg("enable_split", True):
            return
        if self._is_advanced_mode and not self._get_adv_cfg("enable_group_split_adv", True):
            return

        umo = event.unified_msg_origin
        blacklist = self._get_cfg("conversation_blacklist", [])
        whitelist = self._get_cfg("conversation_whitelist", [])
        if umo in blacklist:
            return
        if whitelist and umo not in whitelist:
            return
        if not self._get_cfg("enable_group_split", True) and event.message_obj.group_id:
            return

        split_scope = self._get_cfg("split_scope", "llm_only")
        is_llm_reply = self._is_model_generated_reply(event, result)
        if split_scope == "llm_only" and not is_llm_reply:
            return

        # --- 2. 长度校验 ---
        total_text_len = sum(len(c.text) for c in result.chain if isinstance(c, Plain))
        max_len_no_split = self._get_cfg("max_length_no_split", 0)
        if max_len_no_split > 0 and total_text_len < max_len_no_split:
            return
        max_len_disable = self._get_cfg("max_length_to_disable", 0)
        if max_len_disable > 0 and total_text_len > max_len_disable:
            return

        conv_key = self._get_conversation_key(event)
        lock = self._get_processing_lock(conv_key)
        async with lock:
            await self._do_split_and_send(event, result, is_proactive=False)

    # ------------------------------------------------------------------
    # 主动发送拦截（委托给 splitter.proactive）
    # ------------------------------------------------------------------
    def _install_send_message_patch(self) -> None:
        install_send_message_patch(self)

    async def _handle_proactive_send(self, session, message_chain) -> bool:
        return await handle_proactive_send(self, session, message_chain)

    def _build_synthetic_event(self, session) -> "AstrMessageEvent | None":
        return build_synthetic_event(session)

    def _build_synthetic_result(self, message_chain):
        return build_synthetic_result(message_chain)

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _is_model_generated_reply(self, event: AstrMessageEvent, result: Any) -> bool:
        if not result:
            return False
        is_model_result = getattr(result, "is_model_result", None)
        if callable(is_model_result):
            try:
                return bool(is_model_result())
            except Exception:
                pass
        content_type = getattr(result, "result_content_type", None)
        if content_type is not None:
            type_name = getattr(content_type, "name", "")
            return type_name in {"LLM_RESULT", "AGENT_RUNNER_ERROR", "AGENT_RUNNER_RESULT", "TOOL_RESULT", "TOOL_CALL"}
        return getattr(event, "__is_llm_reply", False)

    def _log_segment(self, index: int, total: int, chain: List[BaseMessageComponent], method: str) -> None:
        content = "".join([c.text if isinstance(c, Plain) else f"[{type(c).__name__}]" for c in chain])
        logger.info("[Splitter] 第 {}/{} 段 ({}): {}".format(index, total, method, content.replace("\n", "\\n")))

    def _trim_segment_edge_blank_lines(self, segment: List[BaseMessageComponent]) -> None:
        f_p = next((c for c in segment if isinstance(c, Plain)), None)
        l_p = next((c for c in reversed(segment) if isinstance(c, Plain)), None)
        if f_p and f_p.text:
            f_p.text = re.sub(r"^(?:[ \t]*\r?\n)+", "", f_p.text)
        if l_p and l_p.text:
            l_p.text = re.sub(r"(?:\r?\n[ \t]*)+$", "", l_p.text)

    async def _send_proactive_segment(
        self,
        event: AstrMessageEvent,
        seg_chain: List[BaseMessageComponent],
        index: int,
        total: int,
    ) -> None:
        """将一个分段通过原始 Context.send_message 发出。

        设置 ContextVar inhibiting 标记，避免被 patch 后的 send_message 再次拦截。
        """
        if not seg_chain:
            return
        text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
        if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain):
            return
        mc = MessageChain()
        mc.chain = seg_chain
        token = self._proactive_inhibit.set(True)
        try:
            original = getattr(self.context, "_splitter_original_send_message", None)
            if original is not None:
                await original(event.unified_msg_origin, mc)
            else:
                await self.context.send_message(event.unified_msg_origin, mc)
        finally:
            self._proactive_inhibit.reset(token)

    # ------------------------------------------------------------------
    # 核心分段与发送逻辑
    # ------------------------------------------------------------------
    async def _do_split_and_send(
        self, event: AstrMessageEvent, result: Any, is_proactive: bool = False
    ) -> None:
        setattr(result, "__splitter_processed", True)
        split_mode = self._get_cfg("split_mode", "regex")
        if self._is_pro_mode:
            split_mode = "regex"

        # --- 2.5. 文本替换 ---
        replace_rules = self._get_cfg("replace_rules", [])
        if replace_rules:
            parsed_rules = []
            for rule in replace_rules:
                if not isinstance(rule, dict):
                    continue
                find = rule.get("find", "")
                if not find:
                    continue
                parsed_rules.append((unescape_replace_str(find), unescape_replace_str(rule.get("replace", ""))))
            if parsed_rules:
                for comp in result.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = apply_replace_rules(comp.text, parsed_rules)

        # --- 3. 分段前清理 ---
        if split_mode == "simple":
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    for item in self._get_cfg("clean_before_items", []):
                        if item:
                            comp.text = comp.text.replace(item, "")
        else:
            regex = self._get_cfg("clean_before_regex", "")
            if regex:
                for comp in result.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = re.sub(regex, "", comp.text, flags=re.DOTALL)

        # 零宽空格脱敏处理
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                comp.text = comp.text.replace("\u200b \u200b", "__ZWSP_DOUBLE__").replace("\u200b", "__ZWSP_SINGLE__")

        # --- 4. 构建分段正则 ---
        if split_mode == "simple":
            chars = self._get_cfg("split_chars", ["。", "？", "！", "?", "!", "；", ";", "\n"])
            processed = []
            for c in chars:
                if not c:
                    continue
                processed.append(re.escape(str(c).replace("\\n", "\n").replace("\\t", "\t")))
            processed.sort(key=len, reverse=True)
            split_pattern = "(?:{})+".format("|".join(processed)) if processed else r"[\n]+"
        else:
            split_pattern = self._get_cfg("split_regex", r"[。？！?!\\\n…]+")

        # --- 4.5. 不分段保护词 ---
        no_split_around = [str(w) for w in self._get_cfg("no_split_around", []) if w]

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
            solo_count = sum(
                1 for c in result.chain
                if not isinstance(c, (Plain, Reply))
                and strategies.get(type(c).__name__.lower(), "default") == "单独"
            )
            target_segs = max(1, max_segs - solo_count)
            if text_weight > 0:
                ideal_length = max(
                    math.ceil(text_weight / target_segs),
                    self._get_cfg("min_segment_length", 10),
                )

        segments = split_chain_smart(
            result.chain,
            split_pattern,
            self._get_cfg("enable_smart_split", True),
            strategies,
            self._get_cfg("enable_reply", True),
            self._get_cfg("enable_smart_reply", False),
            self._get_cfg,
            ideal_length,
            no_split_around,
        )

        # 强制分段上限控制
        if max_segs > 0 and len(segments) > max_segs:
            merged_last: List[BaseMessageComponent] = []
            for seg in segments[max_segs - 1:]:
                merged_last.extend(seg)
            optimized_last: List[BaseMessageComponent] = []
            for comp in merged_last:
                if optimized_last and isinstance(comp, Plain) and isinstance(optimized_last[-1], Plain):
                    optimized_last[-1] = Plain(optimized_last[-1].text + comp.text)
                else:
                    optimized_last.append(comp)
            segments = segments[: max_segs - 1] + [optimized_last]

        # 均分模式尾部合并
        if self._get_cfg("balanced_split_mode", False) and len(segments) >= 2:
            last_text = "".join([c.text for c in segments[-1] if isinstance(c, Plain)]).strip()
            if 0 < len(last_text) < self._get_cfg("min_segment_length", 10):
                if not any(not isinstance(c, (Plain, Reply)) for c in segments[-1]):
                    segments[-2].extend(segments.pop())

        # --- 6. 语音检测与回复处理 ---
        source_id = str(getattr(event.message_obj, "message_id", "") or "")
        enable_reply = self._get_cfg("enable_reply", True)
        enable_smart = self._get_cfg("enable_smart_reply", False)

        # 检查是否将以语音方式回复：
        #   情形 A：TTS 插件已提前将文本转为 Record 组件写入 chain
        #   情形 B：框架内置 TTS 已启用，会在分段发送时将 Plain 转为 Record
        # 主动发送场景没有真实 event 会话，框架内置 TTS 无法判定，因此跳过情形 B。
        plugin_has_voice = chain_has_voice(result.chain)
        framework_will_tts = (
            False if is_proactive
            else await will_use_framework_tts(self.context, event, self._get_cfg("enable_tts_for_segments", True))
        )
        suppress_reply_for_voice = plugin_has_voice or framework_will_tts
        if suppress_reply_for_voice:
            logger.info("[Splitter] 检测到语音输出，已自动屏蔽消息引用（Reply）")

        effective_enable_reply = enable_reply and not suppress_reply_for_voice

        if segments and source_id:
            if enable_smart:
                if should_add_smart_reply(self._message_queues, enable_smart, event) and not suppress_reply_for_voice:
                    prepend_reply(segments[0], source_id)
            elif effective_enable_reply:
                prepend_reply(segments[0], source_id)

        # --- 7. 后处理（清理/还原零宽空格）---
        at_strategy = strategies.get("at", "跟随下段")
        at_needs_proc = at_strategy in ["接下文", "跟随下段", "嵌入"] and any(
            type(c).__name__.lower() == "at" for c in result.chain
        )

        for seg in segments:
            if self._get_cfg("trim_segment_edge_blank_lines", True):
                self._trim_segment_edge_blank_lines(seg)
            for comp in seg:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = (
                        comp.text.replace("__ZWSP_DOUBLE__", "\u200b \u200b")
                        .replace("__ZWSP_SINGLE__", "\u200b")
                    )
                    if split_mode == "simple":
                        for item in self._get_cfg("clean_after_items", []):
                            if item:
                                comp.text = comp.text.replace(item, "")
                    else:
                        regex = self._get_cfg("clean_after_regex", "")
                        if regex:
                            comp.text = re.sub(regex, "", comp.text, flags=re.DOTALL)

        # --- 单段快速路径 ---
        if len(segments) <= 1 and not at_needs_proc:
            final = segments[0] if segments else []
            if enable_smart and not effective_enable_reply:
                final = remove_reply_components(final)
            elif suppress_reply_for_voice:
                final = remove_reply_components(final)
            if chain_has_voice(final):
                final = remove_reply_components(final)
            if is_proactive:
                await self._send_proactive_segment(event, final, 1, 1)
                result.chain.clear()
            else:
                # 被动发送：对单段做 TTS 预处理，若产生语音则自行发出，
                # 以便在发出前确保 Reply 已被移除，避免框架 TTS 转换后带 Reply 造成空引用。
                tts_final = await process_tts_for_segment(
                    self.context, event, final, self._get_cfg("enable_tts_for_segments", True)
                )
                if chain_has_voice(tts_final):
                    tts_final = remove_reply_components(tts_final)
                    self._log_segment(1, 1, tts_final, "分段发送(TTS)")
                    await self._send_proactive_segment(event, tts_final, 1, 1)
                    result.chain.clear()
                else:
                    result.chain.clear()
                    result.chain.extend(final)
            return

        # --- 8. 发送（除最后一段外依次发出）---
        for i in range(len(segments) - 1):
            seg_chain = segments[i]
            if i > 0 and enable_smart and not effective_enable_reply:
                seg_chain = remove_reply_components(seg_chain)
            text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
            if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain):
                continue

            next_text = "".join([c.text for c in segments[i + 1] if isinstance(c, Plain)])
            delay = calculate_delay(self._get_cfg, next_text)

            try:
                if not is_proactive:
                    seg_chain = await process_tts_for_segment(
                        self.context, event, seg_chain, self._get_cfg("enable_tts_for_segments", True)
                    )
                    if chain_has_voice(seg_chain):
                        seg_chain = remove_reply_components(seg_chain)
                # 发送前最终检查：若含语音/音频组件，移除所有 Reply，避免空引用
                if chain_has_voice(seg_chain):
                    seg_chain = remove_reply_components(seg_chain)
                self._log_segment(i + 1, len(segments), seg_chain, "主动发送" if is_proactive else "分段发送")
                await self._send_proactive_segment(event, seg_chain, i + 1, len(segments))
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送失败: {e}")

        if enable_smart and source_id:
            mark_bot_reply(self._message_queues, self._last_smart_reply_mark, event, source_id)

        # --- 最后一段处理 ---
        last_seg = segments[-1]
        if enable_smart and not effective_enable_reply:
            last_seg = remove_reply_components(last_seg)
        elif suppress_reply_for_voice:
            last_seg = remove_reply_components(last_seg)
        if chain_has_voice(last_seg):
            last_seg = remove_reply_components(last_seg)

        if is_proactive:
            self._log_segment(len(segments), len(segments), last_seg, "主动发送")
            await self._send_proactive_segment(event, last_seg, len(segments), len(segments))
            result.chain.clear()
        else:
            # 被动发送：对最后一段做 TTS 预处理，若产生语音则自行发出，
            # 避免框架 TTS 转换后带 Reply 造成空引用。
            tts_last_seg = await process_tts_for_segment(
                self.context, event, last_seg, self._get_cfg("enable_tts_for_segments", True)
            )
            if chain_has_voice(tts_last_seg):
                tts_last_seg = remove_reply_components(tts_last_seg)
                self._log_segment(len(segments), len(segments), tts_last_seg, "分段发送(TTS)")
                await self._send_proactive_segment(event, tts_last_seg, len(segments), len(segments))
                result.chain.clear()
            else:
                result.chain.clear()
                result.chain.extend(last_seg)
