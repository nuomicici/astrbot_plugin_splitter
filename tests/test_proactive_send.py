import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from main import MessageSplitterPlugin


class ProactiveSendTests(unittest.IsolatedAsyncioTestCase):
    """回归测试：主动发送（Context.send_message）应被分段处理。

    框架对主动发送的消息不经过流水线，on_decorating_result 钩子无法触发。
    插件通过劫持 Context.send_message 实现主动发送分段。
    """

    def _make_plugin(self, enable_proactive=True):
        plugin = MessageSplitterPlugin.__new__(MessageSplitterPlugin)
        plugin._is_simple_mode = False
        plugin._is_advanced_mode = False
        plugin._is_pro_mode = True

        def fake_get_cfg(key, default=None):
            if key == "enable_proactive_split":
                return enable_proactive
            return default

        plugin._get_cfg = fake_get_cfg
        plugin._is_model_generated_reply = lambda event, result: True
        plugin._get_processing_lock = lambda key: asyncio.Lock()
        plugin._proactive_inhibit = asyncio.ContextVar(
            "__splitter_proactive_inhibit", default=False
        )
        # 记录原始 send_message 调用
        sent_chains = []
        original_send = AsyncMock(side_effect=lambda umo, mc: sent_chains.append(mc))
        ctx = SimpleNamespace(send_message=original_send)
        plugin.context = ctx
        return plugin, original_send, sent_chains

    async def test_send_message_patched_on_loaded_when_enabled(self):
        plugin, original_send, _ = self._make_plugin(enable_proactive=True)
        plugin._on_loaded()
        # 劫持后 context.send_message 已被替换，且原始实现已保存
        self.assertTrue(getattr(plugin.context, "_splitter_send_patched", False))
        self.assertTrue(
            hasattr(plugin.context, "_splitter_original_send_message")
        )

    async def test_send_message_not_patched_when_disabled(self):
        plugin, original_send, _ = self._make_plugin(enable_proactive=False)
        plugin._on_loaded()
        self.assertFalse(getattr(plugin.context, "_splitter_send_patched", False))
        # send_message 仍为原始引用
        self.assertIs(plugin.context.send_message, original_send)

    async def test_patched_send_message_splits_long_text(self):
        plugin, original_send, sent_chains = self._make_plugin(enable_proactive=True)
        # 让 _do_split_and_send 直接走真实切分（不 mock），但阻止 TTS 与发送
        plugin._will_use_framework_tts = AsyncMock(return_value=False)
        plugin._process_tts_for_segment = AsyncMock(side_effect=lambda e, s: s)
        plugin._mark_bot_reply = MagicMock()

        # 直接走切分+发送：构造一个长文本，验证被拆成多段调用原始 send_message
        long_text = "第一句。第二句。第三句。第四句。"
        mc = MessageChain(chain=[Plain(long_text)])

        # 调用 _do_split_and_send 的主动分支
        syn_event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:GROUP_MESSAGE:100",
            message_obj=SimpleNamespace(message_id="", group_id="100"),
        )
        syn_result = SimpleNamespace(chain=[Plain(long_text)])
        # 设置 result_content_type 让 _is_model_generated_reply 一致
        syn_result.result_content_type = None
        syn_result.is_model_result = lambda: False
        syn_result.is_llm_result = lambda: False

        # 需要让 _get_cfg 在切分阶段返回合理默认值
        def get_cfg(key, default=None):
            vals = {
                "enable_proactive_split": True,
                "split_mode": "simple",
                "split_chars": ["。", "？", "！", "?", "!", "；", ";", "\n"],
                "enable_smart_split": True,
                "balanced_split_mode": False,
                "max_segments": 7,
                "min_segment_length": 0,
                "enable_reply": True,
                "enable_smart_reply": False,
                "image_strategy": "单独",
                "at_strategy": "跟随下段",
                "face_strategy": "嵌入",
                "other_media_strategy": "跟随下段",
                "trim_segment_edge_blank_lines": True,
                "max_length_no_split": 0,
                "max_length_to_disable": 0,
                "no_split_around": [],
                "clean_before_items": [],
                "clean_after_items": [],
                "replace_rules": [],
                "enable_tts_for_segments": False,
                "clean_before_regex": "",
                "clean_after_regex": "",
            }
            return vals.get(key, default)

        plugin._get_cfg = get_cfg

        await plugin._do_split_and_send(syn_event, syn_result, is_proactive=True)

        # 主动发送应至少调用原始 send_message 多次（被分段）
        self.assertGreater(original_send.await_count, 1)
        # syn_result.chain 应被清空（主动发送无 respond stage 托底）
        self.assertEqual(syn_result.chain, [])

    async def test_patched_send_message_reentry_guard(self):
        """分段内部回调原始 send_message 时不应再次进入拦截逻辑。"""
        plugin, original_send, _ = self._make_plugin(enable_proactive=True)
        plugin._on_loaded()
        patched = plugin.context.send_message

        # 模拟分段内部发送：设置 inhibiting 标记后调用 patched
        token = plugin._proactive_inhibit.set(True)
        try:
            await patched("aiocqhttp:GROUP_MESSAGE:1", MessageChain(chain=[Plain("x")]))
        finally:
            plugin._proactive_inhibit.reset(token)

        # 应直接调用 original_send 一次，且 _handle_proactive_send 未被触发
        self.assertEqual(original_send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
