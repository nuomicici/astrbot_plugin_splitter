import unittest
from types import SimpleNamespace

from splitter.qqofficial import (
    C2C_PASSIVE_REPLY_LIMIT,
    GROUP_PASSIVE_REPLY_LIMIT,
    get_passive_reply_limit,
    is_qq_official_event,
    should_send_via_event,
)


class PatchedGroupMessage:  # 与适配器中的类名一致
    pass


class PatchedC2CMessage:
    pass


class PatchedMessage:  # 频道消息
    pass


def make_event(platform="qq_official", raw=None, synthetic=False):
    ev = SimpleNamespace(
        get_platform_name=lambda: platform,
        message_obj=SimpleNamespace(raw_message=raw),
    )
    if synthetic:
        ev._splitter_synthetic = True
    return ev


class QQOfficialAdaptTests(unittest.TestCase):
    def test_detects_both_qq_official_platforms(self):
        self.assertTrue(is_qq_official_event(make_event("qq_official")))
        self.assertTrue(is_qq_official_event(make_event("qq_official_webhook")))
        self.assertFalse(is_qq_official_event(make_event("aiocqhttp")))

    def test_event_without_platform_getter_is_not_qq_official(self):
        self.assertFalse(is_qq_official_event(SimpleNamespace()))

    def test_synthetic_event_never_uses_event_send(self):
        # 合成事件没有真实用户消息，不能被动回复，只能走原主动发送
        ev = make_event(raw=PatchedGroupMessage(), synthetic=True)
        self.assertFalse(should_send_via_event(ev))
        self.assertEqual(get_passive_reply_limit(ev), 0)

    def test_other_platform_unaffected(self):
        ev = make_event("aiocqhttp", raw=PatchedGroupMessage())
        self.assertFalse(should_send_via_event(ev))
        self.assertEqual(get_passive_reply_limit(ev), 0)

    def test_group_limit(self):
        ev = make_event(raw=PatchedGroupMessage())
        self.assertTrue(should_send_via_event(ev))
        self.assertEqual(get_passive_reply_limit(ev), GROUP_PASSIVE_REPLY_LIMIT)
        self.assertEqual(GROUP_PASSIVE_REPLY_LIMIT, 5)

    def test_c2c_limit(self):
        ev = make_event(raw=PatchedC2CMessage())
        self.assertEqual(get_passive_reply_limit(ev), C2C_PASSIVE_REPLY_LIMIT)
        self.assertEqual(C2C_PASSIVE_REPLY_LIMIT, 4)

    def test_guild_has_no_count_limit_but_still_uses_event_send(self):
        ev = make_event(raw=PatchedMessage())
        self.assertTrue(should_send_via_event(ev))
        self.assertEqual(get_passive_reply_limit(ev), 0)

    def test_cron_event_falls_back_to_proactive(self):
        # 定时任务的 CronMessageEvent.raw_message 是字符串，
        # 适配器会丢弃，因此必须回退到主动发送通道
        ev = make_event(raw="定时任务提示文本")
        self.assertFalse(should_send_via_event(ev))
        self.assertEqual(get_passive_reply_limit(ev), 0)

    def test_missing_raw_message_falls_back_to_proactive(self):
        ev = make_event(raw=None)
        self.assertFalse(should_send_via_event(ev))


if __name__ == "__main__":
    unittest.main()
