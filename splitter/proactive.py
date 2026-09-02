# splitter/proactive.py
# 主动发送拦截：monkey-patch Context.send_message，构造合成事件与结果
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

if TYPE_CHECKING:
    pass


def install_send_message_patch(plugin) -> None:
    """劫持 Context.send_message，对主动发送的消息执行分段。

    用 _splitter_original_send_message 保存原始实现，避免重复 patch。
    通过 ContextVar 在分段内部回调原始 send_message 时设置 inhibiting 标记，
    防止形成无限递归。
    """
    ctx = plugin.context
    if getattr(ctx, "_splitter_send_patched", False):
        return
    original = ctx.send_message
    setattr(ctx, "_splitter_original_send_message", original)
    setattr(ctx, "_splitter_send_patched", True)

    async def patched_send_message(session, message_chain):
        if plugin._proactive_inhibit.get():
            return await original(session, message_chain)
        try:
            handled = await handle_proactive_send(plugin, session, message_chain)
            if handled:
                return True
        except Exception:
            logger.error("[Splitter] 主动发送分段失败，回退原发送", exc_info=True)
        return await original(session, message_chain)

    ctx.send_message = patched_send_message
    logger.info("[Splitter] 已劫持 Context.send_message，启用主动发送分段")


async def handle_proactive_send(plugin, session, message_chain) -> bool:
    """处理主动发送的消息。返回 True 表示已处理，False 表示交回原发送逻辑。"""
    if message_chain is None:
        return False
    chain = getattr(message_chain, "chain", None)
    if not chain:
        return False

    umo = str(session)
    blacklist = plugin._get_cfg("conversation_blacklist", [])
    whitelist = plugin._get_cfg("conversation_whitelist", [])
    if umo in blacklist:
        return False
    if whitelist and umo not in whitelist:
        return False
    is_group = ":GroupMessage:" in umo
    if not plugin._get_cfg("enable_group_split", True) and is_group:
        return False

    split_scope = plugin._get_cfg("split_scope", "llm_only")
    if split_scope == "llm_only":
        return False

    total_text_len = sum(len(c.text) for c in chain if isinstance(c, Plain))
    max_len_no_split = plugin._get_cfg("max_length_no_split", 0)
    if max_len_no_split > 0 and total_text_len < max_len_no_split:
        return False
    max_len_disable = plugin._get_cfg("max_length_to_disable", 0)
    if max_len_disable > 0 and total_text_len > max_len_disable:
        return False

    syn_event = build_synthetic_event(session)
    if syn_event is None:
        return False
    syn_result = build_synthetic_result(message_chain)

    from splitter.reply import get_conversation_key
    conv_key = get_conversation_key(syn_event)
    lock = plugin._get_processing_lock(conv_key)
    async with lock:
        await plugin._do_split_and_send(syn_event, syn_result, is_proactive=True)
    return True


def build_synthetic_event(session) -> "AstrMessageEvent | None":
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
        msg.message_id = ""
        msg.message_str = ""
        msg.message = []
        msg.sender = None
        msg.self_id = ""
        msg.type = ses.message_type if isinstance(ses.message_type, MessageType) else MessageType.FRIEND_MESSAGE
        if msg.type and "GROUP" in getattr(msg.type, "value", "").upper():
            msg.group_id = ses.session_id

        class _FakeMeta:
            def __init__(self, pid):
                self.id = pid
                self.name = pid
                self.display_name = pid
                self.description = ""
                self.adapter_display_name = pid
                self.support_streaming_message = False
                self.support_proactive_message = True

        ev = AstrMessageEvent.__new__(AstrMessageEvent)
        ev.message_str = ""
        ev.message_obj = msg
        ev.platform_meta = _FakeMeta(ses.platform_id)
        ev.session = ses
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


def build_synthetic_result(message_chain):
    """将主动发送的 MessageChain 包装为 MessageEventResult，以复用分段逻辑。"""
    from astrbot.api.event import MessageEventResult
    from astrbot.core.message.message_event_result import ResultContentType

    result = MessageEventResult()
    result.chain = list(getattr(message_chain, "chain", []) or [])
    result.use_t2i_ = getattr(message_chain, "use_t2i_", None)
    result.use_markdown_ = getattr(message_chain, "use_markdown_", None)
    result.type = getattr(message_chain, "type", None)
    result.result_content_type = ResultContentType.GENERAL_RESULT
    return result
