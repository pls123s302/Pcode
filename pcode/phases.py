"""相位系统：推导式设计（照搬 MiniCode turn_kernel 的思路）
没有任何状态机——每一步用纯函数重新计算当前相位。
"""


def derive_phase(step: int, max_steps: int) -> tuple:
    """输入第几步和总预算，输出 (相位, 指导语)。
    explore: 第1步；verify: 预算70%线之后或只剩最后1步；其余全是 execute。
    """
    verify_after = max(3, round(max_steps * 0.7))

    if step <= 1:
        return "explore", "inspect, decompose, and anchor the task before committing（先调查、先分解，再动手承诺）"
    if step >= verify_after or (max_steps - step) < 1:
        return "verify", "verify changes, run tests, and finalize only with support（验证改动、跑测试拿证据，有支撑才能收工）"
    return "execute", "prefer concrete tool use and incremental edits（多用工具、增量式修改）"
