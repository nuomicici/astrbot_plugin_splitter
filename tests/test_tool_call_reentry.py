import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Plain

from main import MessageSplitterPlugin


class ToolCallReentryTests(unittest.IsolatedAsyncioTestCase):
    """回归测试：同一 event 上多个 result 都应被分段处理。

    对应 issue #32：Agent 工具调用场景下框架会在同一个 event 上多次
    yield 新的 result（每次工具调用结束后以及最终回复各产生一个全新
    MessageEventResult），event 级重入锁会拦截工具调用后的最终回复。
    """

    def _make_plugin(self):
        plugin = MessageSplitterPlugin.__new__(MessageSplitterPlugin)
        plugin._is_simple_mode = False
        plugin._is_advanced_mode = False
        plugin._get_cfg = lambda key, default=None: default
        plugin._is_model_generated_reply = lambda event, result: True
        plugin._get_processing_lock = lambda key: asyncio.Lock()
        plugin._do_split_and_send = AsyncMock()
        return plugin

    async def test_each_new_result_on_same_event_can_be_processed(self):
        plugin = self._make_plugin()

        first_result = SimpleNamespace(chain=[Plain("工具调用阶段")])
        second_result = SimpleNamespace(chain=[Plain("最终文本回复")])
        event = SimpleNamespace(
            unified_msg_origin="test:friend:1",
            message_obj=SimpleNamespace(group_id=None),
            get_result=lambda: first_result,
        )

        await plugin.on_decorating_result(event)
        event.get_result = lambda: second_result
        await plugin.on_decorating_result(event)

        self.assertEqual(plugin._do_split_and_send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
