# splitter/reply.py
# Reply 组件处理与智能回复队列管理
from collections import defaultdict, deque
from typing import List

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import BaseMessageComponent, Reply


def get_conversation_key(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def get_message_queue(message_queues: defaultdict, event: AstrMessageEvent) -> deque:
    return message_queues[get_conversation_key(event)]


def remember_incoming_message(message_queues: defaultdict, event: AstrMessageEvent) -> None:
    message_id = getattr(event.message_obj, "message_id", None)
    if not message_id:
        return
    queue = get_message_queue(message_queues, event)
    queue.append(str(message_id))
    if len(queue) > 200:
        queue.popleft()


def mark_bot_reply(
    message_queues: defaultdict,
    last_smart_reply_mark: dict,
    event: AstrMessageEvent,
    base_message_id: str,
) -> None:
    if not base_message_id:
        return
    conv_key = get_conversation_key(event)
    mark = "__bot_reply__{}".format(base_message_id)
    queue = message_queues[conv_key]
    if last_smart_reply_mark.get(conv_key) != mark:
        queue.append(mark)
        last_smart_reply_mark[conv_key] = mark
        if len(queue) > 200:
            queue.popleft()


def should_add_smart_reply(
    message_queues: defaultdict,
    enable_smart_reply: bool,
    event: AstrMessageEvent,
) -> bool:
    if not enable_smart_reply:
        return False
    platform_name = str(getattr(event, "get_platform_name", lambda: "")() or "")
    if platform_name.lower() == "dingtalk":
        return False
    message_id = getattr(event.message_obj, "message_id", None)
    if not message_id:
        return False
    queue = get_message_queue(message_queues, event)
    queue_str = [str(x) for x in queue]
    msg_id = str(message_id)
    if msg_id not in queue_str:
        return False
    idx = queue_str.index(msg_id)
    return len(queue_str) - idx - 1 > 0


def has_reply_component(chain: List[BaseMessageComponent]) -> bool:
    return any(isinstance(c, Reply) for c in chain)


def prepend_reply(chain: List[BaseMessageComponent], message_id: str) -> None:
    if message_id and not has_reply_component(chain):
        chain.insert(0, Reply(id=message_id))


def remove_reply_components(chain: List[BaseMessageComponent]) -> List[BaseMessageComponent]:
    return [comp for comp in chain if not isinstance(comp, Reply)]
