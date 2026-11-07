# splitter/qqofficial.py
# QQ 官方机器人平台适配（qq_official / qq_official_webhook）
#
# 背景：
#   分段插件原先通过 Context.send_message 发出非最后一段。该路径最终走
#   Platform.send_by_session，QQ 官方适配器在群聊场景下不会携带 msg_id，
#   等价于「群主动消息」。多数机器人没有群主动消息权限，平台返回
#   40034105（主动消息发送失败，无权限），分段因此发不出去。
#
# 适配方式（依据 QQ 机器人官方文档「消息收发概述」与「发送群聊消息」）：
#   * 被动回复（携带 msg_id）不需要主动消息权限；
#   * 被动回复有效期：群聊 5 分钟 / 单聊 60 分钟；
#   * 每条用户消息可被动回复次数：群聊 5 次 / 单聊 4 次（频道无次数限制）；
#   * 相同 msg_id + msg_seq 重复发送会失败，适配器已使用随机 msg_seq。
#   因此被动场景改走 event.send()（适配器会带上原消息 msg_id），
#   并把分段总数限制在被动回复次数以内（最后一段由框架发出，同样占用一次）。

QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

# 每条用户消息允许的被动回复次数（官方文档）
GROUP_PASSIVE_REPLY_LIMIT = 5
C2C_PASSIVE_REPLY_LIMIT = 4


def is_synthetic_event(event) -> bool:
    """是否为插件为主动发送构造的合成事件（没有真实的用户消息可供被动回复）。"""
    return bool(getattr(event, "_splitter_synthetic", False))


def is_qq_official_event(event) -> bool:
    """事件是否来自 QQ 官方机器人平台。"""
    getter = getattr(event, "get_platform_name", None)
    if not callable(getter):
        return False
    try:
        return str(getter()) in QQ_OFFICIAL_PLATFORMS
    except Exception:
        return False


# QQ 官方适配器只接受这些原生消息对象作为 raw_message，
# 其他来源（如定时任务的 CronMessageEvent、webchat 等）会被适配器丢弃，
# 这类事件不能走 event.send()，必须回退到主动发送通道。
_QQOFFICIAL_RAW_MESSAGE_NAMES = ("GroupMessage", "C2CMessage", "Message", "DirectMessage")


def has_qq_official_raw_message(event) -> bool:
    """raw_message 是否为 QQ 官方适配器可识别的原生消息对象。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is None or isinstance(raw, str):
        return False
    raw_name = type(raw).__name__
    return any(name in raw_name for name in _QQOFFICIAL_RAW_MESSAGE_NAMES)


def should_send_via_event(event) -> bool:
    """分段是否应通过 event.send() 以被动回复方式发出。

    仅当事件来自 QQ 官方平台、是真实用户事件且携带原生消息对象时成立。
    定时任务等合成事件没有可回复的 msg_id，只能走主动发送。
    """
    if not is_qq_official_event(event) or is_synthetic_event(event):
        return False
    return has_qq_official_raw_message(event)


def get_passive_reply_limit(event) -> int:
    """返回当前事件允许的被动回复次数上限，0 表示不需要限制。"""
    if not should_send_via_event(event):
        return 0
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    raw_name = type(raw).__name__
    if "GroupMessage" in raw_name:
        return GROUP_PASSIVE_REPLY_LIMIT
    if "C2CMessage" in raw_name:
        return C2C_PASSIVE_REPLY_LIMIT
    return 0
