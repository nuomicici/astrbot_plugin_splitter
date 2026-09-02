# splitter/tts.py
# TTS 相关：语音检测、框架 TTS 预判、逐段 TTS 处理
import inspect
import random
from typing import List

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import BaseMessageComponent, Plain, Record

try:
    from astrbot.core.star.session_llm_manager import SessionServiceManager
except ImportError:
    SessionServiceManager = None


def chain_has_voice(chain: List[BaseMessageComponent]) -> bool:
    """检查消息链中是否已存在语音组件（Record）。"""
    return any(isinstance(c, Record) for c in chain)


async def will_use_framework_tts(context, event: AstrMessageEvent, enable_tts_for_segments: bool) -> bool:
    """检查框架内置 TTS 是否会对当前会话生效。"""
    if not enable_tts_for_segments:
        return False
    try:
        get_config = getattr(context, "get_config", None)
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
        get_tts = getattr(context, "get_using_tts_provider", None)
        if not callable(get_tts):
            return False
        tts_prov = get_tts(event.unified_msg_origin)
        if not tts_prov:
            return False
        if SessionServiceManager is not None:
            should_tts = getattr(SessionServiceManager, "should_process_tts_request", None)
            if callable(should_tts) and not await should_tts(event):
                return False
        prob = float(tts_cfg.get("trigger_probability", 1.0))
        return prob > 0.0
    except Exception:
        return False


async def process_tts_for_segment(
    context,
    event: AstrMessageEvent,
    segment: List[BaseMessageComponent],
    enable_tts_for_segments: bool,
) -> List[BaseMessageComponent]:
    """将分段中的 Plain 文本通过框架内置 TTS 转换为 Record（若配置启用）。"""
    if not enable_tts_for_segments:
        return segment
    try:
        get_config = getattr(context, "get_config", None)
        if not callable(get_config):
            return segment
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
            return segment
        get_tts = getattr(context, "get_using_tts_provider", None)
        if not callable(get_tts):
            return segment
        tts_prov = get_tts(event.unified_msg_origin)
        if not tts_prov:
            return segment
        if SessionServiceManager is not None:
            should_tts = getattr(SessionServiceManager, "should_process_tts_request", None)
            if callable(should_tts) and not await should_tts(event):
                return segment
        if random.random() > float(tts_cfg.get("trigger_probability", 1.0)):
            return segment
        dual = tts_cfg.get("dual_output", False)
        new_seg = []
        for comp in segment:
            if isinstance(comp, Plain) and len(comp.text) > 1:
                try:
                    path = await tts_prov.get_audio(comp.text)
                    if path:
                        new_seg.append(Record(file=path, url=path))
                        if dual:
                            new_seg.append(comp)
                    else:
                        new_seg.append(comp)
                except Exception:
                    new_seg.append(comp)
            else:
                new_seg.append(comp)
        return new_seg
    except Exception:
        return segment
