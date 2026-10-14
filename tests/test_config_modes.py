import json
import os
import sys
import unittest
from unittest.mock import MagicMock

# 模拟 astrbot.api.logger 依赖以允许独立单元测试运行
if "astrbot.api" not in sys.modules:
    mock_api = MagicMock()
    mock_api.logger = MagicMock()
    sys.modules["astrbot"] = MagicMock()
    sys.modules["astrbot.api"] = mock_api

from splitter.config import (
    apply_advanced_mode_defaults,
    apply_simple_mode_defaults,
    get_adv_cfg,
    get_cfg,
    get_simple_cfg,
    migrate_config,
)


class ConfigModesTests(unittest.TestCase):
    def setUp(self):
        schema_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
        with open(schema_path, "r", encoding="utf-8") as f:
            self.schema = json.load(f)

    def test_schema_structure(self):
        """验证 Schema 的顶层键与模式条件绑定"""
        self.assertIn("config_mode", self.schema)
        self.assertIn("simple_settings", self.schema)
        self.assertIn("adv_basic_settings", self.schema)
        self.assertIn("adv_split_settings", self.schema)
        self.assertIn("adv_clean_settings", self.schema)
        self.assertIn("adv_reply_media_settings", self.schema)
        self.assertIn("adv_delay_settings", self.schema)
        self.assertIn("basic_settings", self.schema)
        self.assertIn("split_settings", self.schema)
        self.assertIn("clean_settings", self.schema)
        self.assertIn("reply_media_settings", self.schema)
        self.assertIn("delay_settings", self.schema)

        # 检查进阶模式的 condition
        adv_groups = [
            "adv_basic_settings", "adv_split_settings", "adv_clean_settings",
            "adv_reply_media_settings", "adv_delay_settings"
        ]
        for grp in adv_groups:
            self.assertEqual(self.schema[grp].get("condition"), {"config_mode": "进阶模式"})

        # 检查补全项
        adv_basic = self.schema["adv_basic_settings"]["items"]
        self.assertIn("max_length_no_split_adv", adv_basic)
        self.assertIn("max_length_to_disable_adv", adv_basic)
        self.assertIn("enable_tts_for_segments_adv", adv_basic)

        adv_split = self.schema["adv_split_settings"]["items"]
        self.assertIn("min_segment_length_adv", adv_split)
        self.assertIn("trim_segment_edge_blank_lines_adv", adv_split)

        adv_reply = self.schema["adv_reply_media_settings"]["items"]
        self.assertIn("enable_reply_adv", adv_reply)
        self.assertIn("enable_smart_reply_adv", adv_reply)
        self.assertIn("at_strategy_adv", adv_reply)
        self.assertIn("face_strategy_adv", adv_reply)
        self.assertIn("other_media_strategy_adv", adv_reply)

        pro_basic = self.schema["basic_settings"]["items"]
        self.assertIn("enable_tts_for_segments", pro_basic)

    def test_simple_mode_defaults(self):
        """测试简易模式默认映射"""
        config = {
            "config_mode": "简易模式",
            "simple_settings": {
                "enable_split": True,
                "max_segments_simple": 5,
                "send_speed": "自然",
                "enable_reply_simple": True,
            }
        }
        overrides = apply_simple_mode_defaults(config)
        self.assertTrue(overrides["enable_group_split"])
        self.assertEqual(overrides["max_segments"], 5)
        self.assertTrue(overrides["enable_reply"])
        self.assertTrue(overrides["enable_tts_for_segments"])

    def test_advanced_mode_modular_reading(self):
        """测试进阶模式模块化分组配置读取与补全项生效"""
        config = {
            "config_mode": "进阶模式",
            "adv_basic_settings": {
                "enable_group_split_adv": True,
                "max_length_no_split_adv": 20,
                "max_length_to_disable_adv": 1000,
                "enable_tts_for_segments_adv": False,
            },
            "adv_split_settings": {
                "split_chars_adv": ["。", "！"],
                "max_segments_adv": 4,
                "min_segment_length_adv": 15,
                "trim_segment_edge_blank_lines_adv": False,
            },
            "adv_clean_settings": {
                "clean_before_items_adv": ["test"],
            },
            "adv_reply_media_settings": {
                "enable_reply_adv": False,
                "enable_smart_reply_adv": True,
                "at_strategy_adv": "单独",
            },
            "adv_delay_settings": {
                "send_speed_adv": "快速",
            }
        }
        overrides = apply_advanced_mode_defaults(config)
        self.assertTrue(overrides["enable_group_split"])
        self.assertEqual(overrides["max_length_no_split"], 20)
        self.assertEqual(overrides["max_length_to_disable"], 1000)
        self.assertFalse(overrides["enable_tts_for_segments"])
        self.assertEqual(overrides["max_segments"], 4)
        self.assertEqual(overrides["min_segment_length"], 15)
        self.assertFalse(overrides["trim_segment_edge_blank_lines"])
        self.assertFalse(overrides["enable_reply"])
        self.assertTrue(overrides["enable_smart_reply"])
        self.assertEqual(overrides["at_strategy"], "单独")
        self.assertEqual(overrides["delay_strategy"], "fixed")
        self.assertEqual(overrides["fixed_delay"], 0.3)

    def test_legacy_advanced_settings_migration(self):
        """测试原有旧版 advanced_settings 字典的平滑迁移与原配置保留"""
        old_config = {
            "config_mode": "进阶模式",
            "advanced_settings": {
                "enable_group_split_adv": False,
                "split_scope_adv": "全部输出",
                "split_chars_adv": ["\n", "；"],
                "clean_before_items_adv": ["冗余文本"],
                "send_speed_adv": "慢速",
            }
        }
        migrate_config(old_config)

        # 检查是否平滑迁移到新分组
        self.assertIn("adv_basic_settings", old_config)
        self.assertFalse(old_config["adv_basic_settings"]["enable_group_split_adv"])
        self.assertEqual(old_config["adv_basic_settings"]["split_scope_adv"], "全部输出")
        self.assertEqual(old_config["adv_split_settings"]["split_chars_adv"], ["\n", "；"])
        self.assertEqual(old_config["adv_clean_settings"]["clean_before_items_adv"], ["冗余文本"])
        self.assertEqual(old_config["adv_delay_settings"]["send_speed_adv"], "慢速")

        # 验证读取与映射正常
        overrides = apply_advanced_mode_defaults(old_config)
        self.assertFalse(overrides["enable_group_split"])
        self.assertEqual(overrides["split_scope"], "all")
        self.assertEqual(overrides["split_chars"], ["\n", "；"])
        self.assertEqual(overrides["clean_before_items"], ["冗余文本"])
        self.assertEqual(overrides["delay_strategy"], "fixed")
        self.assertEqual(overrides["fixed_delay"], 2.5)

    def test_pro_mode_reading(self):
        """测试专业模式配置读取"""
        config = {
            "config_mode": "专业模式",
            "basic_settings": {
                "enable_group_split": True,
                "enable_tts_for_segments": False,
            },
            "split_settings": {
                "split_regex": r"[。？！]+",
            }
        }
        migrate_config(config)
        self.assertTrue(get_cfg(config, is_pro_mode=True, simple_overrides={}, key="enable_group_split"))
        self.assertFalse(get_cfg(config, is_pro_mode=True, simple_overrides={}, key="enable_tts_for_segments"))
        self.assertEqual(get_cfg(config, is_pro_mode=True, simple_overrides={}, key="split_regex"), r"[。？！]+")


if __name__ == "__main__":
    unittest.main()
