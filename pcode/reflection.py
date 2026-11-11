"""任务反思引擎：从 transcript 切片提炼经验（复刻 MiniCode agent_reflection.py）

关键设计：纯启发式、零 LLM——反思每个任务结束都要跑，必须免费；
提炼得糙没关系，质量由策展人和反馈淘汰兜底。
四张网一遍扫过：错误(前缀)/决策(关键词)/事实(参数路径)/工具(名字)。
判定不靠理解靠算术：confidence = (0.8|0.4) − 0.1×错误 + 0.02×工具 ≥ 0.5 才入库。
推论：纯失败的任务数学上永远过不了闸——没有解法的坑不值得存。
"""
import re
import time

# ① 错误网：agent 包异常时写的前缀，闭环自证近乎必中；再兜一个 Traceback
ERROR_PREFIX = "工具执行出错"
ERROR_MARKS = ("Traceback", "Error code")
# ② 决策网：中英关键词，捞 assistant 文本里的"嘴上决定"
_DECISION_KWS = ("决定", "选择", "采用", "接下来", "计划", "改用", "使用",
                 "decide", "choose", "will ", "plan to", "use ")
# ③ 事实网：参数里长得像文件路径的片段（必须有点+扩展名，单独的 "." 不算）
_PATH_RE = re.compile(r"[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9_]{1,8}")

_MAX_TEXT = 300        # 每条文本截断后再扫（信噪比限额：决策总在开头几句）
_CAP_DECISIONS = 5     # 产出硬顶——记忆条尺寸和任务长度彻底脱钩
_CAP_FILES = 10
_CAP_CONTENT = 1200
_MIN_CONFIDENCE = 0.5


def _is_error(result: str) -> bool:
    return result.startswith(ERROR_PREFIX) or any(
        m in result for m in ERROR_MARKS)


def _scan_decisions(text: str, out: list):
    for kw in _DECISION_KWS:
        idx = text.find(kw)
        if idx >= 0:
            # 取关键词所在的那句（往前找句首，往后找句号）
            start = max(text.rfind("。", 0, idx), text.rfind(". ", 0, idx)) + 1
            end = text.find("。", idx)
            sentence = text[start:end if end > idx else len(text)].strip()
            if len(sentence) > 8:
                out.append(sentence[:200])
            return          # 每条文本最多捞一句，防同段重复命中


def reflect(task: str, records: list, success: bool | None = None) -> dict | None:
    """扫一遍本任务的记录切片，产出一条记忆候选。

    records 兼容两种形态（都兜住）：
    - transcript 条目: {kind: user/tool/assistant, name, args, result, text}
    - messages 消息:   {role: user/assistant/tool, content, tool_calls}
    success: 任务出口的真实成败（agent 在两个出口处传入）。这是对 MiniCode
    的关键修正——它用"切片里无错误"推断成功，导致"犯错但最终修好"的黄金
    记忆（坑+解法都在手）永远过不了闸；我们有出口信息，就该用。
    没干过工具活也没说过决策的琐碎任务 → 返回 None（闸门前的初筛）。
    """
    tools, errors, decisions, files = [], [], [], set()
    has_tool = False

    for r in records:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind") or r.get("role")
        if kind == "tool":
            has_tool = True
            name = str(r.get("name") or "tool")
            tools.append(name)
            result = str(r.get("result") or r.get("content") or "")[:200]
            if _is_error(result):
                errors.append(f"{name}: {result[:80]}")
            files.update(_PATH_RE.findall(str(r.get("args") or ""))[:6])
            files.update(_PATH_RE.findall(result)[:6])
        elif kind == "assistant":
            text = str(r.get("text") or r.get("content") or "")[:_MAX_TEXT]
            _scan_decisions(text, decisions)
        elif kind == "system" or kind == "compaction":
            continue         # 框架注入块和事件不入网

    if not has_tool and not decisions:
        return None          # 初筛：琐碎任务（纯问答/寒暄）不值得任何加工

    if success is None:                  # 无外部判定时退回保守推断（MiniCode同款）
        success = not errors
    confidence = (0.8 if success else 0.4) \
        - min(len(errors) * 0.1, 0.3) + min(len(tools) * 0.02, 0.1)
    if confidence < _MIN_CONFIDENCE:
        return None          # 算术闸门：纯失败（没有解法的坑）数学上必被拦下

    # ── 组装 content（MiniCode 同款分节结构）──
    parts = [f"Task Context: {task[:120]}"]
    if files:
        parts.append("Files: " + ", ".join(sorted(files)[:_CAP_FILES]))
    if decisions:
        parts.append("")
        parts.append("Key Decisions:")
        parts += [f"  - {d}" for d in decisions[:_CAP_DECISIONS]]
    if errors:
        parts.append("")
        parts.append("Errors Encountered:")
        parts += [f"  - {e}" for e in errors[:5]]
    parts.append("")
    parts.append("Lessons Learned:")
    if success and errors:
        parts.append("  - 过程中出错但最终解决；下次同类任务可参考错误与解法")
    elif success:
        parts.append("  - 任务顺利完成")
    else:
        parts.append("  - 任务未完成")
    if tools:
        uniq = list(dict.fromkeys(tools))
        parts.append(f"  - 使用工具: {', '.join(uniq[:5])}")

    tags = ["self-reflection", "success" if success else "failure"]
    tags += list(dict.fromkeys(tools))[:5]

    return {
        "category": "task_context" if files else "reflection",
        "content": "\n".join(parts)[:_CAP_CONTENT],
        "tags": tags,
        "confidence": round(confidence, 2),
        "success": success,
        "timestamp": time.time(),
    }
