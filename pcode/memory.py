"""记忆与上下文管理：FULL 压缩 + TOOL_BUDGET 出账前全量扫描
触发条件学 MiniCode：不用消息条数，用估算 token 占预算的比例。
"""
import time
import uuid
from pathlib import Path

COMPACT_THRESHOLD_TOKENS = 16000   # 估算超过即压缩（qwen-plus窗口128K，此值取成本友好位）
KEEP_RECENT = 10                   # 压缩时保留最近N条原文不动

# ── TOOL_BUDGET：出账前扫描，超限工具结果落盘换预览（工具无关、历史无关）──
TOOL_OUTPUT_LIMIT = 2000
ARCHIVE_MARK = "[工具输出已存档"
CACHE_ROOT = Path.home() / ".pcode" / "cache"
_current_session = "default"


def set_session(session_id: str):
    """cli 启动/切换会话时注入，落盘文件按会话分文件夹（GC 好整删）"""
    global _current_session
    _current_session = session_id or "default"


PREVIEW_MAX_CHARS = 500            # 预览总封顶（MiniCode同款）：防"无换行巨行"把预览撑肥


def _persist_large(content: str, tool_name: str = "tool", meta: str = "") -> str:
    """全文落盘（带自描述元数据头，对齐MiniCode），返回替换用的'存档通知+头尾预览'"""
    d = CACHE_ROOT / _current_session
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{tool_name}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.txt"
    #        ↑ 工具名入文件名              ↑ 毫秒时间戳  ↑ uuid后缀：同毫秒落盘不碰撞
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    f.write_text(f"tool: {tool_name}\ntime: {ts}\n{meta}\n---CONTENT---\n{content}",
                 encoding="utf-8")

    lines = content.splitlines()
    preview = "\n".join(lines[:8])
    if len(lines) > 12:
        preview += f"\n... (省略 {len(lines) - 11} 行) ...\n" + "\n".join(lines[-3:])
    preview = preview[:PREVIEW_MAX_CHARS]
    return (f"{ARCHIVE_MARK} — 原 {len(content)} 字符，完整内容: {f}]\n"
            f"--- 预览（头尾行） ---\n{preview or '（无输出）'}")


def tool_budget_sweep(messages: list) -> list:
    """扫描工具结果，超限的落盘换预览（原地修改并返回）。
    关键规则：只外置'最后一个assistant消息之前'的旧结果——
    它之后的tool消息是模型即将使用的新鲜证据，截胡它 = 弄瞎下一步的编辑。
    已是存档通知的跳过——否则每步扫描会反复落盘预览本身。"""
    last_assistant = -1
    for i, m in enumerate(messages):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        if role == "assistant":
            last_assistant = i

    # 建 tool_call_id → 调用信息 的映射，让落盘档案自带"这是谁的输出"
    calls = {}
    for m in messages:
        tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        for tc in (tcs or []):
            fn = tc.get("function") if isinstance(tc, dict) else getattr(tc.function, "__dict__", {})
            calls[tc.get("id") if isinstance(tc, dict) else tc.id] = fn or {}

    for i, m in enumerate(messages):
        if i > last_assistant:            # 新鲜证据区，跳过
            continue
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        content = m.get("content") or ""
        if not isinstance(content, str) or len(content) <= TOOL_OUTPUT_LIMIT:
            continue
        if content.startswith(ARCHIVE_MARK):
            continue
        fn = calls.get(m.get("tool_call_id"), {})
        name = fn.get("name", "tool")
        meta = f"args: {fn.get('arguments', '')}"
        m["content"] = _persist_large(content, tool_name=name, meta=meta)
    return messages


def estimate_tokens(text: str) -> int:
    """粗估 token：中英混排按 3字符≈1token（不精确但稳定，够触发器用）"""
    return max(1, len(text) // 3)


def _msg_field(m, name, default=None):
    """兼容 dict 和 SDK 对象两种形态"""
    if isinstance(m, dict):
        return m.get(name, default)
    return getattr(m, name, default)


def estimate_messages_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        content = _msg_field(m, "content") or ""
        total += estimate_tokens(str(content))
        if _msg_field(m, "tool_calls"):
            total += 50                     # tool_calls 结构开销粗估
    return total


def _flatten(messages: list) -> str:
    """把旧消息压成摘要提示词用的纯文本，每条截300字控住体积"""
    lines = []
    for m in messages:
        role = _msg_field(m, "role", "?")
        content = str(_msg_field(m, "content") or "")
        if _msg_field(m, "tool_calls"):
            content += "（发起工具调用）"
        lines.append(f"{role}: {content[:300]}")
    return "\n".join(lines)


def _safe_boundary(messages: list, start: int) -> int:
    """配对保护：切点若落在 assistant(tool_calls) 和 tool 结果中间，会 API 400。
    把切点后移，直到第一个 kept 消息是 user 或不带tool_calls的 assistant。"""
    while start < len(messages):
        m = messages[start]
        role = _msg_field(m, "role", "")
        if role == "tool":
            start += 1
            continue
        if role == "assistant" and _msg_field(m, "tool_calls"):
            start += 1
            continue
        return start
    return start


def compact_messages(llm, messages: list,
                     threshold: int = COMPACT_THRESHOLD_TOKENS,
                     keep_recent: int = KEEP_RECENT) -> list:
    """超过阈值时：旧消息请模型总结成[历史摘要]，最近N条原文保留。
    返回新的消息列表（不修改传入列表）。"""
    if estimate_messages_tokens(messages) <= threshold:
        return messages
    if len(messages) <= keep_recent + 2:
        return messages

    start = _safe_boundary(messages, len(messages) - keep_recent)
    old, recent = messages[:start], messages[start:]
    if not old:
        return messages

    prompt = f"""请把以下对话历史压缩成一段中文摘要，供AI助手快速回忆。
要求：
1. 保留关键事实：文件路径、数字、报错信息、任务状态、用户的偏好和要求
2. 丢弃寒暄和过程性细节（工具调用的中间输出）
3. 直接输出摘要文本，不要任何解释

【对话历史】
{_flatten(old)}"""

    summary = llm.chat([{"role": "user", "content": prompt}]).content

    new_messages = [{
        "role": "user",
        "content": f"[历史摘要] 以下是更早对话的压缩记录，请结合它理解后续请求：\n{summary}",
    }]
    new_messages.extend(recent)
    return new_messages
