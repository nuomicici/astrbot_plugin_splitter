# splitter/config.py
# 配置读取、迁移与模式预设逻辑
from typing import Any

from astrbot.api import logger


def get_cfg(config: dict, is_pro_mode: bool, simple_overrides: dict, key: str, default: Any = None) -> Any:
    """从嵌套或扁平结构中获取配置项。简易/进阶模式下优先从 simple_overrides 获取预设值。"""
    if not is_pro_mode and key in simple_overrides:
        return simple_overrides[key]

    categories = [
        "basic_settings", "split_settings", "clean_settings",
        "reply_media_settings", "delay_settings"
    ]
    for cat in categories:
        cat_obj = config.get(cat)
        if isinstance(cat_obj, dict) and key in cat_obj:
            return cat_obj[key]

    return config.get(key, default)


def get_simple_cfg(config: dict, key: str, default: Any = None) -> Any:
    """从简易设置分组中获取配置项。"""
    simple = config.get("simple_settings")
    if isinstance(simple, dict) and key in simple:
        return simple[key]
    return default


def get_adv_cfg(config: dict, key: str, default: Any = None) -> Any:
    """从进阶设置分组中获取配置项。"""
    adv = config.get("advanced_settings")
    if isinstance(adv, dict) and key in adv:
        return adv[key]
    return default


def apply_simple_mode_defaults(config: dict) -> dict:
    """将简易模式的用户友好配置映射为内部参数，返回 overrides 字典。"""
    overrides: dict = {}

    overrides["enable_group_split"] = get_simple_cfg(config, "enable_split", True)
    overrides["enable_proactive_split"] = get_simple_cfg(config, "enable_proactive_split_simple", True)
    overrides["max_segments"] = get_simple_cfg(config, "max_segments_simple", 5)
    overrides["inject_kaomoji_prompt"] = get_simple_cfg(config, "protect_emoji", False)

    img = get_simple_cfg(config, "image_handling", "单独发")
    overrides["image_strategy"] = "单独" if img == "单独发" else "跟随下段"
    overrides["clean_before_items"] = get_simple_cfg(config, "remove_texts_simple", [])

    speed = get_simple_cfg(config, "send_speed", "自然")
    if speed == "快速":
        overrides["delay_strategy"] = "fixed"
        overrides["fixed_delay"] = 0.3
    elif speed == "慢速":
        overrides["delay_strategy"] = "fixed"
        overrides["fixed_delay"] = 2.5
    else:
        overrides["delay_strategy"] = "linear"
        overrides["linear_base"] = 0.5
        overrides["linear_factor"] = 0.1

    overrides["split_mode"] = "simple"
    overrides["split_chars"] = ["。", "？", "！", "?", "!", "；", ";", "\n"]
    overrides["enable_smart_split"] = True
    overrides["balanced_split_mode"] = True
    overrides["split_scope"] = "llm_only"
    overrides["enable_reply"] = get_simple_cfg(config, "enable_reply_simple", True)
    overrides["enable_smart_reply"] = False
    overrides["at_strategy"] = "跟随下段"
    overrides["face_strategy"] = "嵌入"
    overrides["other_media_strategy"] = "跟随下段"
    overrides["trim_segment_edge_blank_lines"] = True
    overrides["max_length_no_split"] = 0
    overrides["max_length_to_disable"] = 0
    overrides["min_segment_length"] = 10
    overrides["balanced_split_ratio_min"] = 0.4
    overrides["balanced_split_ratio_max"] = 0.9

    logger.info("[Splitter] 当前为简易模式，已应用预设配置")
    return overrides


def apply_advanced_mode_defaults(config: dict) -> dict:
    """将进阶模式的配置映射为内部参数，返回 overrides 字典。"""
    overrides: dict = {}

    overrides["enable_group_split"] = get_adv_cfg(config, "enable_group_split_adv", True)
    overrides["enable_proactive_split"] = get_adv_cfg(config, "enable_proactive_split_adv", True)
    scope = get_adv_cfg(config, "split_scope_adv", "仅AI回复")
    overrides["split_scope"] = "llm_only" if scope == "仅AI回复" else "all"

    overrides["split_mode"] = "simple"
    overrides["split_chars"] = get_adv_cfg(config, "split_chars_adv", ["。", "？", "！", "?", "!", "；", ";", "\n"])
    overrides["no_split_around"] = get_adv_cfg(config, "no_split_around_adv", [])
    overrides["max_segments"] = get_adv_cfg(config, "max_segments_adv", 7)
    overrides["enable_smart_split"] = True
    overrides["balanced_split_mode"] = get_adv_cfg(config, "balanced_split_adv", True)
    overrides["min_segment_length"] = 10
    overrides["balanced_split_ratio_min"] = 0.4
    overrides["balanced_split_ratio_max"] = 0.9
    overrides["trim_segment_edge_blank_lines"] = True

    overrides["clean_before_items"] = get_adv_cfg(config, "clean_before_items_adv", [])
    overrides["clean_after_items"] = get_adv_cfg(config, "clean_after_items_adv", [])
    overrides["inject_kaomoji_prompt"] = get_adv_cfg(config, "inject_kaomoji_prompt_adv", False)
    overrides["replace_rules"] = get_adv_cfg(config, "replace_rules_adv", [])
    overrides["reverse_replace"] = get_adv_cfg(config, "reverse_replace_adv", False)

    overrides["image_strategy"] = get_adv_cfg(config, "image_strategy_adv", "单独")
    overrides["at_strategy"] = "跟随下段"
    overrides["face_strategy"] = "嵌入"
    overrides["other_media_strategy"] = "跟随下段"

    overrides["enable_reply"] = True
    overrides["enable_smart_reply"] = False

    overrides["conversation_blacklist"] = get_adv_cfg(config, "conversation_blacklist_adv", [])
    overrides["conversation_whitelist"] = get_adv_cfg(config, "conversation_whitelist_adv", [])

    speed = get_adv_cfg(config, "send_speed_adv", "自然")
    if speed == "快速":
        overrides["delay_strategy"] = "fixed"
        overrides["fixed_delay"] = 0.3
    elif speed == "慢速":
        overrides["delay_strategy"] = "fixed"
        overrides["fixed_delay"] = 2.5
    else:
        overrides["delay_strategy"] = "linear"
        overrides["linear_base"] = 0.5
        overrides["linear_factor"] = 0.1

    overrides["max_length_no_split"] = 0
    overrides["max_length_to_disable"] = 0

    logger.info("[Splitter] 当前为进阶模式，已应用预设配置")
    return overrides


def migrate_config(config: dict) -> None:
    """处理旧版本配置数据类型冲突及嵌套迁移，就地修改 config。"""
    # 1. 键名迁移: clean_items -> clean_before_items
    if "clean_items" in config and "clean_before_items" not in config:
        logger.info("[Splitter] 迁移旧配置项 clean_items 至 clean_before_items")
        config["clean_before_items"] = config.pop("clean_items")

    # 1.5. 为旧 replace_rules 数据补充 __template_key
    for rules_key in ["replace_rules", "replace_rules_adv"]:
        rules = config.get(rules_key)
        if not isinstance(rules, list):
            for cat in ["clean_settings", "advanced_settings"]:
                cat_obj = config.get(cat)
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

    # 2. 结构迁移：将顶层扁平配置移入嵌套对象
    mapping = {
        "simple_settings": ["enable_split", "max_segments_simple", "send_speed", "protect_emoji",
                            "image_handling", "enable_reply_simple", "enable_proactive_split_simple",
                            "remove_texts_simple"],
        "advanced_settings": ["enable_group_split_adv", "enable_proactive_split_adv", "split_scope_adv",
                              "split_chars_adv", "no_split_around_adv", "max_segments_adv", "balanced_split_adv",
                              "clean_before_items_adv", "clean_after_items_adv", "inject_kaomoji_prompt_adv",
                              "replace_rules_adv", "reverse_replace_adv", "send_speed_adv", "image_strategy_adv",
                              "conversation_blacklist_adv", "conversation_whitelist_adv"],
        "basic_settings": ["enable_group_split", "enable_proactive_split", "split_scope",
                           "max_length_no_split", "max_length_to_disable",
                           "conversation_blacklist", "conversation_whitelist"],
        "split_settings": ["split_mode", "split_chars", "split_regex", "no_split_around",
                           "enable_smart_split", "balanced_split_mode", "max_segments",
                           "min_segment_length", "balanced_split_ratio_min", "balanced_split_ratio_max",
                           "trim_segment_edge_blank_lines"],
        "clean_settings": ["clean_before_items", "clean_after_items", "clean_before_regex",
                           "clean_after_regex", "inject_kaomoji_prompt", "replace_rules", "reverse_replace"],
        "reply_media_settings": ["enable_smart_reply", "enable_reply", "image_strategy",
                                 "at_strategy", "face_strategy", "other_media_strategy"],
        "delay_settings": ["delay_strategy", "linear_base", "linear_factor", "log_base",
                           "log_factor", "random_min", "random_max", "fixed_delay"],
    }

    list_fields = {
        "split_chars", "clean_before_items", "clean_after_items",
        "conversation_blacklist", "conversation_whitelist", "remove_texts_simple",
        "split_chars_adv", "no_split_around", "no_split_around_adv",
        "clean_before_items_adv", "clean_after_items_adv",
        "conversation_blacklist_adv", "conversation_whitelist_adv",
    }

    for cat, keys in mapping.items():
        if cat not in config or not isinstance(config[cat], dict):
            config[cat] = {}
        for key in keys:
            if key in config and key != cat:
                val = config.pop(key)
                if key in list_fields:
                    if isinstance(val, str):
                        val = [val] if key != "split_chars" else list(val)
                    elif isinstance(val, list):
                        val = [str(i) for i in val if i is not None]
                config[cat][key] = val
