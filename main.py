# main.py
import re
import math
import random
import asyncio
import contextvars
import inspect
from collections import defaultdict, deque
from typing import List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except ImportError:
    SessionServiceManager = None


class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # --- 1. 配置兼容性与迁移逻辑 ---
        self._migrate_config()

        # --- 1.5. 模式判定与预设映射 ---
        self._config_mode = self.config.get("config_mode", "简易模式")
        self._is_simple_mode = (self._config_mode == "简易模式")
        self._is_advanced_mode = (self._config_mode == "进阶模式")
        self._is_pro_mode = (self._config_mode == "专业模式")
        if self._is_simple_mode:
            self._apply_simple_mode_defaults()
        elif self._is_advanced_mode:
            self._apply_advanced_mode_defaults()

        # 智能回复：按会话缓存消息 ID，供发送前判断"是否被新消息插嘴"
        self._message_queues = defaultdict(deque)
        self._last_smart_reply_mark = {}
        # 防止同一对话并发分段处理导致重复发送
        self._processing_locks: Dict[str, asyncio.Lock] = {}

        # 主动发送拦截：防止拦截器自身调用原始 send_message 时形成无限递归。
        # 使用 ContextVar 做任务级隔离，避免多会话并发互相干扰。
        self._proactive_inhibit = contextvars.ContextVar("__splitter_proactive_inhibit", default=False)

        # 定义成对出现的字符，在智能分段时避免在这些符号内部切断
        self.pair_map = {
            '"': '"', "《": "》", "（": "）", "(": ")",
            "[": "]", "{": "}", "'": "'", "【": "】", "「": "」", "『": "』", "<": ">",
        }
        # 定义引用/引号字符
        self.quote_chars = {'"', "'", "`"}
        self.secondary_pattern = re.compile(r"[，,、；;]+")

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
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
        self._install_send_message_patch()

    def _get_cfg(self, key: str, default: Any = None) -> Any:
        """
        助手函数：自动从嵌套或扁平结构中获取配置项。
        简易/进阶模式下优先从 _simple_overrides 获取预设值。
        """
        # 0. 简易/进阶模式：优先使用预设覆盖值
        if not self._is_pro_mode and hasattr(self, "_simple_overrides"):
            if key in self._simple_overrides:
                return self._simple_overrides[key]

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

    def _get_simple_cfg(self, key: str, default: Any = None) -> Any:
        """从简易设置分组中获取配置项。"""
        simple = self.config.get("simple_settings")
        if isinstance(simple, dict) and key in simple:
            return simple[key]
        return default

    def _get_adv_cfg(self, key: str, default: Any = None) -> Any:
        """从进阶设置分组中获取配置项。"""
        adv = self.config.get("advanced_settings")
        if isinstance(adv, dict) and key in adv:
            return adv[key]
        return default

    def _apply_simple_mode_defaults(self):
        """
        将简易模式的用户友好配置映射为内部参数。
        """
        self._simple_overrides = {}

        # --- 开关 ---
        self._simple_overrides["enable_group_split"] = self._get_simple_cfg("enable_split", True)
        self._simple_overrides["enable_proactive_split"] = self._get_simple_cfg("enable_proactive_split_simple", True)

        # --- 最大段数 ---
        self._simple_overrides["max_segments"] = self._get_simple_cfg("max_segments_simple", 5)

        # --- 颜文字保护（默认关闭）---
        self._simple_overrides["inject_kaomoji_prompt"] = self._get_simple_cfg("protect_emoji", False)

        # --- 图片策略 ---
        img = self._get_simple_cfg("image_handling", "单独发")
        self._simple_overrides["image_strategy"] = "单独" if img == "单独发" else "跟随下段"

        # --- 删除特定文本 -> clean_before_items ---
        self._simple_overrides["clean_before_items"] = self._get_simple_cfg("remove_texts_simple", [])

        # --- 发送节奏 ---
        speed = self._get_simple_cfg("send_speed", "自然")
        if speed == "快速":
            self._simple_overrides["delay_strategy"] = "fixed"
            self._simple_overrides["fixed_delay"] = 0.3
        elif speed == "慢速":
            self._simple_overrides["delay_strategy"] = "fixed"
            self._simple_overrides["fixed_delay"] = 2.5
        else:  # 自然
            self._simple_overrides["delay_strategy"] = "linear"
            self._simple_overrides["linear_base"] = 0.5
            self._simple_overrides["linear_factor"] = 0.1

        # --- 简易模式固定预设 ---
        self._simple_overrides["split_mode"] = "simple"
        self._simple_overrides["split_chars"] = ["。", "？", "！", "?", "!", "；", ";", "\n"]
        self._simple_overrides["enable_smart_split"] = True
        self._simple_overrides["balanced_split_mode"] = True
        self._simple_overrides["split_scope"] = "llm_only"
        self._simple_overrides["enable_reply"] = self._get_simple_cfg("enable_reply_simple", True)
        self._simple_overrides["enable_smart_reply"] = False
        self._simple_overrides["at_strategy"] = "跟随下段"
        self._simple_overrides["face_strategy"] = "嵌入"
        self._simple_overrides["other_media_strategy"] = "跟随下段"
        self._simple_overrides["trim_segment_edge_blank_lines"] = True
        self._simple_overrides["max_length_no_split"] = 0
        self._simple_overrides["max_length_to_disable"] = 0
        self._simple_overrides["min_segment_length"] = 10
        self._simple_overrides["balanced_split_ratio_min"] = 0.4
        self._simple_overrides["balanced_split_ratio_max"] = 0.9

        logger.info("[Splitter] 当前为简易模式，已应用预设配置")

    def _apply_advanced_mode_defaults(self):
        """
        将进阶模式的配置映射为内部参数。
        进阶模式使用列表配置分段符号和清理文本，不使用正则。
        """
        self._simple_overrides = {}

        # --- 基础 ---
        self._simple_overrides["enable_group_split"] = self._get_adv_cfg("enable_group_split_adv", True)
        self._simple_overrides["enable_proactive_split"] = self._get_adv_cfg("enable_proactive_split_adv", True)
        scope = self._get_adv_cfg("split_scope_adv", "仅AI回复")
        self._simple_overrides["split_scope"] = "llm_only" if scope == "仅AI回复" else "all"

        # --- 分段：列表模式 ---
        self._simple_overrides["split_mode"] = "simple"
        self._simple_overrides["split_chars"] = self._get_adv_cfg("split_chars_adv", ["。", "？", "！", "?", "!", "；", ";", "\n"])
        self._simple_overrides["no_split_around"] = self._get_adv_cfg("no_split_around_adv", [])
        self._simple_overrides["max_segments"] = self._get_adv_cfg("max_segments_adv", 7)
        self._simple_overrides["enable_smart_split"] = True
        self._simple_overrides["balanced_split_mode"] = self._get_adv_cfg("balanced_split_adv", True)
        self._simple_overrides["min_segment_length"] = 10
        self._simple_overrides["balanced_split_ratio_min"] = 0.4
        self._simple_overrides["balanced_split_ratio_max"] = 0.9
        self._simple_overrides["trim_segment_edge_blank_lines"] = True

        # --- 清理：列表模式 ---
        self._simple_overrides["clean_before_items"] = self._get_adv_cfg("clean_before_items_adv", [])
        self._simple_overrides["clean_after_items"] = self._get_adv_cfg("clean_after_items_adv", [])
        self._simple_overrides["inject_kaomoji_prompt"] = self._get_adv_cfg("inject_kaomoji_prompt_adv", False)
        self._simple_overrides["replace_rules"] = self._get_adv_cfg("replace_rules_adv", [])
        self._simple_overrides["reverse_replace"] = self._get_adv_cfg("reverse_replace_adv", False)

        # --- 图片 ---
        self._simple_overrides["image_strategy"] = self._get_adv_cfg("image_strategy_adv", "单独")
        self._simple_overrides["at_strategy"] = "跟随下段"
        self._simple_overrides["face_strategy"] = "嵌入"
        self._simple_overrides["other_media_strategy"] = "跟随下段"

        # --- 回复 ---
        self._simple_overrides["enable_reply"] = True
        self._simple_overrides["enable_smart_reply"] = False

        # --- 黑白名单 ---
        self._simple_overrides["conversation_blacklist"] = self._get_adv_cfg("conversation_blacklist_adv", [])
        self._simple_overrides["conversation_whitelist"] = self._get_adv_cfg("conversation_whitelist_adv", [])

        # --- 延迟 ---
        speed = self._get_adv_cfg("send_speed_adv", "自然")
        if speed == "快速":
            self._simple_overrides["delay_strategy"] = "fixed"
            self._simple_overrides["fixed_delay"] = 0.3
        elif speed == "慢速":
            self._simple_overrides["delay_strategy"] = "fixed"
            self._simple_overrides["fixed_delay"] = 2.5
        else:
            self._simple_overrides["delay_strategy"] = "linear"
            self._simple_overrides["linear_base"] = 0.5
            self._simple_overrides["linear_factor"] = 0.1

        # --- 其他固定值 ---
        self._simple_overrides["max_length_no_split"] = 0
        self._simple_overrides["max_length_to_disable"] = 0

        logger.info("[Splitter] 当前为进阶模式，已应用预设配置")

    def _migrate_config(self):
        """
        处理旧版本配置数据类型冲突及嵌套迁移。
        防止用户升级插件后配置"丢失"。
        """
        # 1. 键名迁移: clean_items -> clean_before_items
        if "clean_items" in self.config and "clean_before_items" not in self.config:
            logger.info("[Splitter] 迁移旧配置项 clean_items 至 clean_before_items")
            self.config["clean_before_items"] = self.config.pop("clean_items")

        # 1.5. 为旧 replace_rules 数据补充 __template_key（兼容旧 schema）
        for rules_key in ["replace_rules", "replace_rules_adv"]:
            rules = self.config.get(rules_key)
            if not isinstance(rules, list):
                # 也检查嵌套结构
                for cat in ["clean_settings", "advanced_settings"]:
                    cat_obj = self.config.get(cat)
                    if isinstance(cat_obj, dict):
                        rules = cat_obj.get(rules_key)
                        if isinstance(rules, list):
                            for rule in rules:
                                if isinstance(rule, dict) and "__template_key" not in rule:
                                    rule["__template_key"] = "replace_rule"
            elif isinstance(rules, list):
                for rule in rules:
                    if isinstance(rule, dict) and "__template_key" not in rule:
                        rule["__template_key"] = "replace_rule"

        # 2. 结构迁移：将顶层的扁平配置移动到嵌套对象中
        mapping = {
            "simple_settings": ["enable_split", "max_segments_simple", "send_speed", "protect_emoji", "image_handling", "enable_reply_simple", "enable_proactive_split_simple", "remove_texts_simple"],
            "advanced_settings": ["enable_group_split_adv", "enable_proactive_split_adv", "split_scope_adv", "split_chars_adv", "no_split_around_adv", "max_segments_adv", "balanced_split_adv", "clean_before_items_adv", "clean_after_items_adv", "inject_kaomoji_prompt_adv", "replace_rules_adv", "reverse_replace_adv", "send_speed_adv", "image_strategy_adv", "conversation_blacklist_adv", "conversation_whitelist_adv"],
            "basic_settings": ["enable_group_split", "enable_proactive_split", "split_scope", "max_length_no_split", "max_length_to_disable", "conversation_blacklist", "conversation_whitelist"],
            "split_settings": ["split_mode", "split_chars", "split_regex", "no_split_around", "enable_smart_split", "balanced_split_mode", "max_segments", "min_segment_length", "balanced_split_ratio_min", "balanced_split_ratio_max", "trim_segment_edge_blank_lines"],
            "clean_settings": ["clean_before_items", "clean_after_items", "clean_before_regex", "clean_after_regex", "inject_kaomoji_prompt", "replace_rules", "reverse_replace"],
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
                    list_fields = ["split_chars", "clean_before_items", "clean_after_items", "conversation_blacklist", "conversation_whitelist", "remove_texts_simple", "split_chars_adv", "no_split_around", "no_split_around_adv", "clean_before_items_adv", "clean_after_items_adv", "conversation_blacklist_adv", "conversation_whitelist_adv"]
                    if key in list_fields:
                        if isinstance(val, str):
                            val = [val] if key != "split_chars" else list(val)
                        elif isinstance(val, list):
                            val = [str(i) for i in val if i is not None]
                    self.config[cat][key] = val

    def _get_processing_lock(self, conv_key: str) -> asyncio.Lock:
        if conv_key not in self._processing_locks:
            self._processing_locks[conv_key] = asyncio.Lock()
        return self._processing_locks[conv_key]

    @staticmethod
    def _unescape_replace_str(s: str) -> str:
        """将替换规则中的转义符 \\n \\t \\s 转换为实际字符。"""
        return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\s", " ")

    @staticmethod
    def _apply_replace_rules(text: str, rules: list) -> str:
        """同时应用所有替换规则，避免顺序执行导致的交叉覆盖。

        先用占位符标记所有待替换的位置，最后一次性替换为目标文本，
        使得规则 [1→2, 2→1] 能正确交换而不会互相覆盖。
        """
        if not rules:
            return text
        # 构建一个合并的正则，按查找串长度降序排列以优先匹配较长的
        sorted_rules = sorted(rules, key=lambda r: len(r[0]), reverse=True)
        find_to_replace = {r[0]: r[1] for r in sorted_rules}
        pattern = "|".join(re.escape(r[0]) for r in sorted_rules)
        if not pattern:
            return text
        return re.sub(pattern, lambda m: find_to_replace[m.group()], text)

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

    def _chain_has_voice(self, chain: List[BaseMessageComponent]) -> bool:
        """检查消息链中是否已存在语音组件（Record）。

        用于判断是否有 TTS 插件提前将文本转换为语音，
        此时应屏蔽消息引用（Reply），避免语音消息带引用时发送异常或功能混乱。
        """
        return any(isinstance(c, Record) for c in chain)

    async def _will_use_framework_tts(self, event: AstrMessageEvent) -> bool:
        """检查框架内置 TTS 是否会对当前会话生效。

        复用与 _process_tts_for_segment 相同的判断逻辑，
        在实际转换前预判是否将产生语音输出，用于提前屏蔽消息引用。
        """
        if not self._get_cfg("enable_tts_for_segments", True):
            return False
        try:
            get_config = getattr(self.context, "get_config", None)
            if not callable(get_config):
                return False
            try:
                cfg_sig = inspect.signature(get_config)
                cfg_params = [p for p in cfg_sig.parameters.values() if p.default is inspect.Parameter.empty]
                if len(cfg_params) >= 1:
                    all_cfg = get_config(event.unified_msg_origin)
                else:
                    all_cfg = get_config()
            except (ValueError, TypeError):
                all_cfg = get_config(event.unified_msg_origin)
            tts_cfg = all_cfg.get("provider_tts_settings", {})
            if not tts_cfg.get("enable", False):
                return False
            get_tts = getattr(self.context, "get_using_tts_provider", None)
            if not callable(get_tts):
                return False
            tts_prov = get_tts(event.unified_msg_origin)
            if not tts_prov:
                return False
            if SessionServiceManager is not None:
                should_tts = getattr(SessionServiceManager, "should_process_tts_request", None)
                if callable(should_tts) and not await should_tts(event):
                    return False
            # 只要触发概率大于 0，就认为可能会转 TTS，保守地屏蔽引用
            prob = float(tts_cfg.get("trigger_probability", 1.0))
            return prob > 0.0
        except Exception:
            return False

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
        # 构建反向规则：原规则 find→replace，反向为 replace→find
        reverse_rules = []
        for rule in replace_rules:
            if not isinstance(rule, dict):
                continue
            find = rule.get("find", "")
            replace = rule.get("replace", "")
            if not find or not replace:
                continue
            reverse_rules.append((
                self._unescape_replace_str(replace),
                self._unescape_replace_str(find),
            ))
        if not reverse_rules:
            return
        # 对用户的 prompt 文本执行反向替换
        if hasattr(req, "prompt") and req.prompt:
            req.prompt = self._apply_replace_rules(req.prompt, reverse_rules)

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
        # 仅使用 result 级别的锁防止同一 result 对象被重复处理。
        # 注意：不能使用 event 级别的锁，因为 Agent 工具调用场景下
        # 框架会在同一个 event 对象上多次 yield 新的 result（每次工具调用
        # 结束后以及最终回复各产生一个全新的 MessageEventResult 实例），
        # event 级别的锁会导致工具调用后的最终 LLM 回复不被分段。
        # 参见：https://github.com/nuomicici/astrbot_plugin_splitter/issues/32
        if getattr(result, "__splitter_processed", False): return

        # --- 1. 基础校验 ---
        # 简易模式下，enable_split 为 False 时完全禁用分段
        if self._is_simple_mode and not self._get_simple_cfg("enable_split", True):
            return
        # 进阶模式下，enable_group_split_adv 为 False 时完全禁用分段
        if self._is_advanced_mode and not self._get_adv_cfg("enable_group_split_adv", True):
            return

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

        # 使用 per-conversation 锁防止并发分段处理导致重复发送
        conv_key = self._get_conversation_key(event)
        lock = self._get_processing_lock(conv_key)
        async with lock:
            await self._do_split_and_send(event, result, is_proactive=False)

    # ------------------------------------------------------------------
    # 主动发送拦截（monkey-patch Context.send_message）
    # ------------------------------------------------------------------
    def _install_send_message_patch(self):
        """劫持 Context.send_message，对主动发送的消息执行分段。

        用 _splitter_original_send_message 保存原始实现，避免重复 patch。
        通过 ContextVar 在分段内部回调原始 send_message 时设置 inhibiting 标记，
        防止形成无限递归。
        """
        ctx = self.context
        if getattr(ctx, "_splitter_send_patched", False):
            return
        original = ctx.send_message
        setattr(ctx, "_splitter_original_send_message", original)
        setattr(ctx, "_splitter_send_patched", True)

        async def patched_send_message(session, message_chain):
            # 命中 inhibiting 标记，说明是分段内部回调原始发送，直接放行
            if self._proactive_inhibit.get():
                return await original(session, message_chain)
            try:
                handled = await self._handle_proactive_send(session, message_chain)
                if handled:
                    return True
            except Exception:
                logger.error("[Splitter] 主动发送分段失败，回退原发送", exc_info=True)
            return await original(session, message_chain)

        ctx.send_message = patched_send_message
        logger.info("[Splitter] 已劫持 Context.send_message，启用主动发送分段")

    async def _handle_proactive_send(self, session, message_chain) -> bool:
        """处理主动发送的消息。返回 True 表示已处理，False 表示交回原发送逻辑。

        Args:
            session: unified_msg_origin 字符串或 MessageSesion 对象。
            message_chain: MessageChain 对象。
        """
        if message_chain is None:
            return False
        chain = getattr(message_chain, "chain", None)
        if not chain:
            return False

        # 黑白名单 / 总开关（简易模式关闭时整体不拦截）
        umo = str(session)
        blacklist = self._get_cfg("conversation_blacklist", [])
        whitelist = self._get_cfg("conversation_whitelist", [])
        if umo in blacklist:
            return False
        if whitelist and umo not in whitelist:
            return False
        # 仅根据 umo 判断是否为群聊（格式 平台id:GroupMessage:会话ID，message_type.value 大小写为 GroupMessage）
        is_group = ":GroupMessage:" in umo
        if not self._get_cfg("enable_group_split", True) and is_group:
            return False

        # 分段范围：主动发送的消息链没有 result_content_type 标记，
        # 无法可靠判断是否为 LLM 输出。当 split_scope=llm_only 时，
        # 为尊重"仅 LLM 分段"的配置语义，跳过主动发送分段。
        # 若需要对主动发送分段，请将 split_scope 设为"全部输出"。
        split_scope = self._get_cfg("split_scope", "llm_only")
        if split_scope == "llm_only":
            return False

        # 仅当存在文本时才可能分段
        total_text_len = sum(len(c.text) for c in chain if isinstance(c, Plain))
        max_len_no_split = self._get_cfg("max_length_no_split", 0)
        if max_len_no_split > 0 and total_text_len < max_len_no_split:
            return False
        max_len_disable = self._get_cfg("max_length_to_disable", 0)
        if max_len_disable > 0 and total_text_len > max_len_disable:
            return False

        # 构造合成事件 + 合成结果，复用 _do_split_and_send
        syn_event = self._build_synthetic_event(session)
        if syn_event is None:
            return False
        syn_result = self._build_synthetic_result(message_chain)

        # 复用同一会话的并发锁，避免与被动分段相互冲突
        conv_key = self._get_conversation_key(syn_event)
        lock = self._get_processing_lock(conv_key)
        async with lock:
            await self._do_split_and_send(syn_event, syn_result, is_proactive=True)
        return True

    def _build_synthetic_event(self, session):
        """根据 session 构造一个最小可用的合成 AstrMessageEvent。

        使用 __new__ 创建实例后，手动初始化 _do_split_and_send 实际访问的属性。
        注意：必须直接给 self.session 赋值 MessageSession 实例，
        不能使用 self.session_id 属性（其 setter 依赖 self.session，
        在未初始化时会抛 AttributeError）。
        """
        try:
            from astrbot.core.platform.astrbot_message import AstrBotMessage
            from astrbot.core.platform.message_session import MessageSesion
            from astrbot.core.platform.astr_message_event import AstrMessageEvent
            from astrbot.core.platform.message_type import MessageType

            if isinstance(session, str):
                ses = MessageSesion.from_str(session)
            else:
                ses = session

            msg = AstrBotMessage()
            msg.session_id = ses.session_id
            msg.message_id = ""  # 主动发送无源消息 ID
            msg.message_str = ""
            msg.message = []
            msg.sender = None
            msg.self_id = ""
            # __init__ 会校验 message_type，必须是合法 MessageType
            msg.type = ses.message_type if isinstance(ses.message_type, MessageType) else MessageType.FRIEND_MESSAGE
            if msg.type and "GROUP" in getattr(msg.type, "value", "").upper():
                msg.group_id = ses.session_id

            # platform_meta 仅用于 get_platform_name / get_platform_id 等只读访问，
            # 用一个最小占位对象避免 AttributeError（不继承 PlatformMetadata 抽象类）。
            class _FakeMeta:
                def __init__(self, pid):
                    self.id = pid
                    self.name = pid
                    self.display_name = pid
                    self.description = ""
                    self.adapter_display_name = pid
                    self.support_streaming_message = False
                    self.support_proactive_message = True

            # 直接 __new__ 绕过 __init__（__init__ 内部会构造 TraceSpan 等额外对象，
            # 且不同框架版本实现可能不一致）。手动赋值所需属性：
            #   - self.session       : MessageSession 实例（unified_msg_origin 由它派生）
            #   - self.message_obj   : AstrBotMessage
            #   - self.platform_meta  : 占位元数据
            #   - self.message_str    : 空字符串
            #   - self._result        : None（_do_split_and_send 不读）
            #   - self._extras        : 字典（部分框架方法可能访问）
            ev = AstrMessageEvent.__new__(AstrMessageEvent)
            ev.message_str = ""
            ev.message_obj = msg
            ev.platform_meta = _FakeMeta(ses.platform_id)
            # 直接赋值 MessageSession 实例，避免触发 session_id 属性 setter
            ev.session = ses
            # back compatibility
            ev.platform = ev.platform_meta
            ev._result = None
            ev._extras = {}
            ev._has_send_oper = False
            ev._force_stopped = False
            ev.plugins_name = None
            return ev
        except Exception:
            logger.error("[Splitter] 构造合成事件失败", exc_info=True)
            return None

    def _build_synthetic_result(self, message_chain):
        """将主动发送的 MessageChain 包装为 MessageEventResult，以复用分段逻辑。"""
        from astrbot.api.event import MessageEventResult
        from astrbot.core.message.message_event_result import ResultContentType

        result = MessageEventResult()
        result.chain = list(getattr(message_chain, "chain", []) or [])
        result.use_t2i_ = getattr(message_chain, "use_t2i_", None)
        result.use_markdown_ = getattr(message_chain, "use_markdown_", None)
        result.type = getattr(message_chain, "type", None)
        # 主动发送不视为 LLM 结果：当 split_scope=llm_only 时不会触发，保持原行为
        result.result_content_type = ResultContentType.GENERAL_RESULT
        return result

    async def _do_split_and_send(self, event: AstrMessageEvent, result, is_proactive: bool = False):
        setattr(result, "__splitter_processed", True)
        split_mode = self._get_cfg("split_mode", "regex")
        # 专业模式强制使用正则
        if self._is_pro_mode:
            split_mode = "regex"

        # --- 2.5. 文本替换 ---
        replace_rules = self._get_cfg("replace_rules", [])
        if replace_rules:
            # 预处理所有规则
            parsed_rules = []
            for rule in replace_rules:
                if not isinstance(rule, dict): continue
                find = rule.get("find", "")
                if not find: continue
                replace = rule.get("replace", "")
                parsed_rules.append((self._unescape_replace_str(find), self._unescape_replace_str(replace)))
            if parsed_rules:
                for comp in result.chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = self._apply_replace_rules(comp.text, parsed_rules)

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
            solo_count = sum(1 for c in result.chain if not isinstance(c, (Plain, Reply)) and strategies.get(type(c).__name__.lower(), "default") == "单独")
            target_segs = max(1, max_segs - solo_count)
            if text_weight > 0:
                ideal_length = max(math.ceil(text_weight / target_segs), self._get_cfg("min_segment_length", 10))

        segments = self.split_chain_smart(result.chain, split_pattern, self._get_cfg("enable_smart_split", True), strategies, self._get_cfg("enable_reply", True), ideal_length, no_split_around)

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

        # --- 6. 语音检测与回复处理 ---
        source_id = str(getattr(event.message_obj, "message_id", "") or "")
        enable_reply = self._get_cfg("enable_reply", True)
        enable_smart = self._get_cfg("enable_smart_reply", False)

        # 检查是否将以语音方式回复：
        #   情形 A：TTS 插件（如 fish-speech 等）已提前将文本转为 Record 组件写入 chain
        #   情形 B：框架内置 TTS 已启用，会在分段发送时将 Plain 转为 Record
        # 以上任一情形下，均屏蔽消息引用（Reply），避免语音消息带引用时发送异常或功能混乱。
        # 主动发送场景没有真实 event 会话，框架内置 TTS 无法判定，因此跳过情形 B。
        plugin_has_voice = self._chain_has_voice(result.chain)
        framework_will_tts = False if is_proactive else await self._will_use_framework_tts(event)
        suppress_reply_for_voice = plugin_has_voice or framework_will_tts
        if suppress_reply_for_voice:
            logger.info("[Splitter] 检测到语音输出，已自动屏蔽消息引用（Reply）")

        # 实际的引用开关：同时受"语音屏蔽"影响
        effective_enable_reply = enable_reply and not suppress_reply_for_voice

        if segments and source_id:
            if enable_smart:
                if self._should_add_smart_reply(event) and not suppress_reply_for_voice:
                    self._prepend_reply(segments[0], source_id)
            elif effective_enable_reply:
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
            if enable_smart and not effective_enable_reply:
                final = self._remove_reply_components(final)
            elif suppress_reply_for_voice:
                final = self._remove_reply_components(final)
            if is_proactive:
                # 主动发送没有后续 respond stage 托底，需自行发出单段
                await self._send_proactive_segment(event, final, 1, 1)
                result.chain.clear()
            else:
                result.chain.clear(); result.chain.extend(final)
            return

        # --- 8. 发送 ---
        for i in range(len(segments) - 1):
            seg_chain = segments[i]
            if i > 0 and enable_smart and not effective_enable_reply:
                seg_chain = self._remove_reply_components(seg_chain)
            text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
            if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain): continue
            
            # 延迟基于下一段文字长度计算，模拟"正在打下一段"的真人节奏
            next_text = "".join([c.text for c in segments[i + 1] if isinstance(c, Plain)])
            delay = self.calculate_delay(next_text)

            try:
                if not is_proactive:
                    seg_chain = await self._process_tts_for_segment(event, seg_chain)
                    # 框架 TTS 转换后若产生了 Record，也需要确保 Reply 已被移除
                    if self._chain_has_voice(seg_chain):
                        seg_chain = self._remove_reply_components(seg_chain)
                self._log_segment(i + 1, len(segments), seg_chain, "主动发送" if is_proactive else "分段发送")
                await self._send_proactive_segment(event, seg_chain, i + 1, len(segments))
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送失败: {e}")

        if enable_smart and source_id: self._mark_bot_reply(event, source_id)

        last_seg = segments[-1]
        if enable_smart and not effective_enable_reply:
            last_seg = self._remove_reply_components(last_seg)
        elif suppress_reply_for_voice:
            last_seg = self._remove_reply_components(last_seg)
        if is_proactive:
            # 主动发送：最后一段也需自行发出，发出后清空 chain
            self._log_segment(len(segments), len(segments), last_seg, "主动发送")
            await self._send_proactive_segment(event, last_seg, len(segments), len(segments))
            result.chain.clear()
        else:
            # 被动发送：最后一段留给框架的 respond stage 发出
            result.chain.clear(); result.chain.extend(last_seg)

    async def _send_proactive_segment(self, event: AstrMessageEvent, seg_chain: List[BaseMessageComponent], index: int, total: int):
        """将一个分段通过原始 Context.send_message 发出。

        设置 ContextVar inhibiting 标记，避免被 patch 后的 send_message 再次拦截。
        """
        if not seg_chain:
            return
        text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
        if not text_content.strip(" \t\r\n\u200b") and not any(not isinstance(c, Plain) for c in seg_chain):
            return
        mc = MessageChain(); mc.chain = seg_chain
        token = self._proactive_inhibit.set(True)
        try:
            original = getattr(self.context, "_splitter_original_send_message", None)
            if original is not None:
                await original(event.unified_msg_origin, mc)
            else:
                await self.context.send_message(event.unified_msg_origin, mc)
        finally:
            self._proactive_inhibit.reset(token)

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
            get_config = getattr(self.context, "get_config", None)
            if not callable(get_config): return segment
            # 新版 get_config 可能需要 1 个参数（umo），旧版可能不需要
            try:
                cfg_sig = inspect.signature(get_config)
                cfg_params = [p for p in cfg_sig.parameters.values() if p.default is inspect.Parameter.empty]
                if len(cfg_params) >= 1:
                    all_cfg = get_config(event.unified_msg_origin)
                else:
                    all_cfg = get_config()
            except (ValueError, TypeError):
                all_cfg = get_config(event.unified_msg_origin)
            tts_cfg = all_cfg.get("provider_tts_settings", {})
            if not tts_cfg.get("enable", False): return segment
            get_tts = getattr(self.context, "get_using_tts_provider", None)
            if not callable(get_tts): return segment
            tts_prov = get_tts(event.unified_msg_origin)
            if not tts_prov: return segment
            if SessionServiceManager is not None:
                should_tts = getattr(SessionServiceManager, "should_process_tts_request", None)
                if callable(should_tts) and not await should_tts(event): return segment
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

    def split_chain_smart(self, chain: List[BaseMessageComponent], pattern: str, smart: bool, strategies: Dict[str, str], enable_reply: bool, ideal: int = 0, no_split_around: list = None) -> List[List[BaseMessageComponent]]:
        segments = []; buffer = []; weight = 0
        for comp in chain:
            if isinstance(comp, Plain):
                if not comp.text: continue
                if not smart: self._process_text_simple(comp.text, pattern, segments, buffer, no_split_around); weight = 0
                else: weight = self._process_text_smart(comp.text, pattern, segments, buffer, weight, ideal, no_split_around)
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

    def _process_text_simple(self, text: str, pattern: str, segments: list, buffer: list, no_split_around: list = None):
        parts = re.split("({})".format(pattern), text)
        tmp = ""
        for p in parts:
            if not p: continue
            if re.fullmatch(pattern, p):
                # 检查不分段保护词：分隔符前面的文本或后面紧接的文本包含保护词时，不切分
                if no_split_around and self._is_near_protected_word(tmp, p, parts, parts.index(p), no_split_around):
                    tmp += p
                else:
                    tmp += p; buffer.append(Plain(tmp))
                    segments.append(buffer[:]); buffer.clear(); tmp = ""
            else: tmp += p
        if tmp: buffer.append(Plain(tmp))

    @staticmethod
    def _is_near_protected_word(before_text: str, delim: str, parts: list, delim_idx: int, protected: list) -> bool:
        """判断分隔符之后是否紧邻保护词（simple 模式）。"""
        # 收集分隔符之后的下一个非分隔符文本
        after_text = ""
        for k in range(delim_idx + 1, len(parts)):
            if parts[k]:
                after_text = parts[k]
                break
        # 跳过 after_text 开头的空白
        after_stripped = after_text.lstrip(' \t')
        for word in protected:
            if not word: continue
            wl = len(word)
            # 后文开头（跳过空白后）以保护词开始
            if after_stripped[:wl] == word:
                return True
        return False

    def _process_text_smart(self, text: str, pattern: str, segments: list, buffer: list, start_w: int = 0, ideal: int = 0, no_split_around: list = None) -> int:
        stack = []; compiled = re.compile(pattern); i = 0; n = len(text); chunk = ""; weight = start_w
        ratio_min = self._get_cfg("balanced_split_ratio_min", 0.4)
        ratio_max = self._get_cfg("balanced_split_ratio_max", 0.9)
        
        while i < n:
            if text.startswith("```", i) and (i == 0 or text[i-1] == '\n'):
                idx = text.find("```", i + 3)
                if idx != -1: chunk += text[i:idx+3]; weight += idx+3-i; i = idx+3; continue
                else: chunk += text[i:]; weight += n-i; break
            if text.startswith("<think>", i) and (i == 0 or text[i-1] == '\n'):
                idx = text.find("</think>", i + 7)
                if idx != -1: chunk += text[i:idx+8]; weight += idx+8-i; i = idx+8; continue
                else: chunk += text[i:]; weight += n-i; break

            # Markdown 表格保护：检测以 | 开头的连续行，将整个表格作为一个整体
            if (i == 0 or text[i-1] == '\n') and i < n and text[i] == '|':
                table_end = i
                pos = i
                while pos < n:
                    line_end = text.find('\n', pos)
                    if line_end == -1: line_end = n
                    line = text[pos:line_end].strip()
                    if line.startswith('|') or (line and all(c in '-| :' for c in line)):
                        table_end = line_end + 1 if line_end < n else n
                        pos = table_end
                    else:
                        break
                if table_end > i + 1:
                    table_text = text[i:table_end]
                    chunk += table_text; weight += sum(1 for c in table_text if not c.isspace()); i = table_end; continue

            match = compiled.match(text, pos=i)
            if match:
                delim = match.group(); should = False
                if not stack or "\n" in delim:
                    should = True
                    if ideal > 0 and weight < ideal * ratio_min: should = False
                    if should and "\n" not in delim and re.match(r"^[ \t.?!,;:\-']+$", delim):
                        p_c = text[i-1] if i > 0 else ""; n_c = text[i+len(delim)] if i+len(delim) < n else ""
                        # 纯英文上下文保护：前后都是 ASCII 字母/数字/标点
                        if re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", p_c) and re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", n_c): should = False
                        # 中英文（含数字）混排空格保护：分隔符仅为空白时，仅当前后字符为不同文字体系（一侧 CJK，一侧 Latin/数字）才不分割
                        if should and re.match(r"^[ \t]+$", delim):
                            cjk_re = r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]"
                            lat_re = r"[a-zA-Z0-9]"
                            if p_c and n_c:
                                p_is_cjk = bool(re.match(cjk_re, p_c))
                                p_is_lat = bool(re.match(lat_re, p_c))
                                n_is_cjk = bool(re.match(cjk_re, n_c))
                                n_is_lat = bool(re.match(lat_re, n_c))
                                # 仅在跨文字体系（CJK-Latin 或 Latin-CJK）混排时保护空格不分段
                                if (p_is_cjk and n_is_lat) or (p_is_lat and n_is_cjk): should = False
                    # 不分段保护词：保护词出现在分隔符之后（即将成为下一段开头）时不切分
                    if should and no_split_around:
                        after_pos = i + len(delim)
                        # 跳过分隔符后的空白字符，定位到实际文本
                        scan_pos = after_pos
                        while scan_pos < n and text[scan_pos] in ' \t':
                            scan_pos += 1
                        for word in no_split_around:
                            if not word: continue
                            wl = len(word)
                            if scan_pos + wl <= n and text[scan_pos:scan_pos + wl] == word:
                                should = False; break
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
                if stack and stack[-1] == char: 
                    stack.pop()
                elif not stack: 
                    stack.append(char)
            elif not stack and char in self.pair_map: stack.append(char)
            elif stack and char == self.pair_map.get(stack[-1]): stack.pop()
            
            chunk += char; i += 1; weight += 1 if not char.isspace() else 0
        if chunk: buffer.append(Plain(chunk))
        return weight
