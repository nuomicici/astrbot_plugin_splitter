# splitter/splitter.py
# 文本切分核心算法与延迟计算
import math
import random
import re
from typing import Dict, List, Optional

from astrbot.api.message_components import BaseMessageComponent, Plain


# 成对出现的字符，智能分段时避免在这些符号内部切断
PAIR_MAP: Dict[str, str] = {
    '"': '"', "《": "》", "（": "）", "(": ")",
    "[": "]", "{": "}", "'": "'", "【": "】", "「": "」", "『": "』", "<": ">",
}
# 引用/引号字符
QUOTE_CHARS = {'"', "'", "`"}
SECONDARY_PATTERN = re.compile(r"[，,、；;]+")


def unescape_replace_str(s: str) -> str:
    """将替换规则中的转义符 \\n \\t \\s 转换为实际字符。"""
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\s", " ")


def apply_replace_rules(text: str, rules: list) -> str:
    """同时应用所有替换规则，避免顺序执行导致的交叉覆盖。"""
    if not rules:
        return text
    sorted_rules = sorted(rules, key=lambda r: len(r[0]), reverse=True)
    find_to_replace = {r[0]: r[1] for r in sorted_rules}
    pattern = "|".join(re.escape(r[0]) for r in sorted_rules)
    if not pattern:
        return text
    return re.sub(pattern, lambda m: find_to_replace[m.group()], text)


def calculate_delay(cfg_getter, text: str) -> float:
    """根据延迟策略计算分段间隔时间（秒）。"""
    strategy = cfg_getter("delay_strategy", "linear")
    if strategy == "random":
        return random.uniform(cfg_getter("random_min", 1.0), cfg_getter("random_max", 3.0))
    if strategy == "log":
        return min(cfg_getter("log_base", 0.5) + cfg_getter("log_factor", 0.8) * math.log(len(text) + 1), 5.0)
    if strategy == "linear":
        return cfg_getter("linear_base", 0.5) + len(text) * cfg_getter("linear_factor", 0.1)
    return cfg_getter("fixed_delay", 1.5)


def split_chain_smart(
    chain: List[BaseMessageComponent],
    pattern: str,
    smart: bool,
    strategies: Dict[str, str],
    enable_reply: bool,
    enable_smart_reply: bool,
    cfg_getter,
    ideal: int = 0,
    no_split_around: Optional[list] = None,
) -> List[List[BaseMessageComponent]]:
    """将消息链按分段策略拆分为多个分段列表。"""
    segments: list = []
    buffer: list = []
    weight = 0

    for comp in chain:
        if isinstance(comp, Plain):
            if not comp.text:
                continue
            if not smart:
                _process_text_simple(comp.text, pattern, segments, buffer, no_split_around)
                weight = 0
            else:
                weight = _process_text_smart(comp.text, pattern, segments, buffer, cfg_getter, weight, ideal, no_split_around)
        else:
            c_type = type(comp).__name__.lower()
            if "reply" in c_type:
                if enable_reply or enable_smart_reply:
                    buffer.append(comp)
                continue
            strategy = strategies.get(c_type, strategies.get("default", "跟随下段"))
            if strategy == "单独":
                if buffer:
                    segments.append(buffer[:])
                    buffer.clear()
                segments.append([comp])
                weight = 0
            elif strategy == "跟随上段":
                if buffer:
                    buffer.append(comp)
                    segments.append(buffer[:])
                    buffer.clear()
                    weight = 0
                elif segments:
                    segments[-1].append(comp)
                else:
                    segments.append([comp])
            elif strategy in ["跟随下段", "接下文"]:
                if buffer:
                    segments.append(buffer[:])
                    buffer.clear()
                    weight = 0
                buffer.append(comp)
            else:
                buffer.append(comp)

    if buffer:
        segments.append(buffer)
    return [s for s in segments if s]


def _process_text_simple(
    text: str,
    pattern: str,
    segments: list,
    buffer: list,
    no_split_around: Optional[list] = None,
) -> None:
    parts = re.split("({})".format(pattern), text)
    tmp = ""
    for p in parts:
        if not p:
            continue
        if re.fullmatch(pattern, p):
            if no_split_around and _is_near_protected_word(tmp, p, parts, parts.index(p), no_split_around):
                tmp += p
            else:
                tmp += p
                buffer.append(Plain(tmp))
                segments.append(buffer[:])
                buffer.clear()
                tmp = ""
        else:
            tmp += p
    if tmp:
        buffer.append(Plain(tmp))


def _is_near_protected_word(
    before_text: str,
    delim: str,
    parts: list,
    delim_idx: int,
    protected: list,
) -> bool:
    """判断分隔符之后是否紧邻保护词（simple 模式）。"""
    after_text = ""
    for k in range(delim_idx + 1, len(parts)):
        if parts[k]:
            after_text = parts[k]
            break
    after_stripped = after_text.lstrip(" \t")
    for word in protected:
        if not word:
            continue
        if after_stripped[: len(word)] == word:
            return True
    return False


def _process_text_smart(
    text: str,
    pattern: str,
    segments: list,
    buffer: list,
    cfg_getter,
    start_w: int = 0,
    ideal: int = 0,
    no_split_around: Optional[list] = None,
) -> int:
    """智能分段：处理成对符号、代码块、表格保护，返回累计权重。"""
    stack: list = []
    compiled = re.compile(pattern)
    i = 0
    n = len(text)
    chunk = ""
    weight = start_w
    ratio_min = cfg_getter("balanced_split_ratio_min", 0.4)
    ratio_max = cfg_getter("balanced_split_ratio_max", 0.9)

    while i < n:
        # 代码块保护
        if text.startswith("```", i) and (i == 0 or text[i - 1] == "\n"):
            idx = text.find("```", i + 3)
            if idx != -1:
                chunk += text[i : idx + 3]
                weight += idx + 3 - i
                i = idx + 3
            else:
                chunk += text[i:]
                weight += n - i
            continue

        # <think> 块保护
        if text.startswith("<think>", i) and (i == 0 or text[i - 1] == "\n"):
            idx = text.find("</think>", i + 7)
            if idx != -1:
                chunk += text[i : idx + 8]
                weight += idx + 8 - i
                i = idx + 8
            else:
                chunk += text[i:]
                weight += n - i
            continue

        # Markdown 表格保护
        if (i == 0 or text[i - 1] == "\n") and i < n and text[i] == "|":
            table_end = i
            pos = i
            while pos < n:
                line_end = text.find("\n", pos)
                if line_end == -1:
                    line_end = n
                line = text[pos:line_end].strip()
                if line.startswith("|") or (line and all(c in "-| :" for c in line)):
                    table_end = line_end + 1 if line_end < n else n
                    pos = table_end
                else:
                    break
            if table_end > i + 1:
                table_text = text[i:table_end]
                chunk += table_text
                weight += sum(1 for c in table_text if not c.isspace())
                i = table_end
                continue

        match = compiled.match(text, pos=i)
        if match:
            delim = match.group()
            should = False
            if not stack or "\n" in delim:
                should = True
                if ideal > 0 and weight < ideal * ratio_min:
                    should = False
                if should and "\n" not in delim and re.match(r"^[ \t.?!,;:\-']+$", delim):
                    p_c = text[i - 1] if i > 0 else ""
                    n_c = text[i + len(delim)] if i + len(delim) < n else ""
                    if re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", p_c) and re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", n_c):
                        should = False
                    if should and re.match(r"^[ \t]+$", delim):
                        cjk_re = r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]"
                        lat_re = r"[a-zA-Z0-9]"
                        if p_c and n_c:
                            p_is_cjk = bool(re.match(cjk_re, p_c))
                            p_is_lat = bool(re.match(lat_re, p_c))
                            n_is_cjk = bool(re.match(cjk_re, n_c))
                            n_is_lat = bool(re.match(lat_re, n_c))
                            if (p_is_cjk and n_is_lat) or (p_is_lat and n_is_cjk):
                                should = False
                if should and no_split_around:
                    after_pos = i + len(delim)
                    scan_pos = after_pos
                    while scan_pos < n and text[scan_pos] in " \t":
                        scan_pos += 1
                    for word in no_split_around:
                        if not word:
                            continue
                        wl = len(word)
                        if scan_pos + wl <= n and text[scan_pos : scan_pos + wl] == word:
                            should = False
                            break
            if should:
                chunk += delim
                buffer.append(Plain(chunk))
                segments.append(buffer[:])
                buffer.clear()
                chunk = ""
                weight = 0
                i += len(delim)
            else:
                chunk += delim
                weight += len(delim)
                i += len(delim)
            continue

        if ideal > 0 and weight >= ideal * ratio_max and not stack:
            sec = SECONDARY_PATTERN.match(text, pos=i)
            if sec:
                delim = sec.group()
                chunk += delim
                buffer.append(Plain(chunk))
                segments.append(buffer[:])
                buffer.clear()
                chunk = ""
                weight = 0
                i += len(delim)
                continue

        char = text[i]
        if char in QUOTE_CHARS:
            if stack and stack[-1] == char:
                stack.pop()
            elif not stack:
                stack.append(char)
        elif not stack and char in PAIR_MAP:
            stack.append(char)
        elif stack and char == PAIR_MAP.get(stack[-1]):
            stack.pop()

        chunk += char
        i += 1
        weight += 1 if not char.isspace() else 0

    if chunk:
        buffer.append(Plain(chunk))
    return weight
