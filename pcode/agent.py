"""Agent 主循环：Function Calling 版 ReAct + 相位系统 + 步数预算 + 会话"""
import json

from pcode.llm import LLM
from pcode.memory import (COMPACT_THRESHOLD_TOKENS, estimate_messages_tokens,
                          compact_messages, tool_budget_sweep)
from pcode.phases import derive_phase
from pcode.tool import ToolCollection


class Agent:
    def __init__(self, llm: LLM, tools: ToolCollection, max_steps: int = 50, on_phase=None):
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps          # 预算是框架的账本，和模型无关
        self.on_phase = on_phase            # 相位变化时通知界面

    def run(self, task: str, session=None, on_tool=None) -> str:
        """执行一个任务直到完成。
        session: 传入则消息历史进会话（跨回合记忆），不传则任务独立
        on_tool: 每执行一个工具通知一次 on_tool(描述行, 结果预览)
        """
        def _remember(m):
            """消息入账的唯一入口：有会话走append（内含SDK对象→dict转换），
            无会话直接进本地列表。严禁绕过它append生对象——json.dumps会炸"""
            if session is not None:
                session.append(m)
            else:
                messages.append(m)

        if session is not None:
            messages = session.get()                       # 拿到会话的活历史
            _remember({"role": "user", "content": task})
            session.append_transcript("user", text=task)   # 流水账同步记一笔
        else:
            messages = [{"role": "user", "content": task}]

        schemas = self.tools.to_openai_schemas()
        last_phase = None

        for step in range(1, self.max_steps + 1):
            # ── 出账前优化管线（每次调模型前，复刻MiniCode process_request）──
            messages = tool_budget_sweep(messages)     # ① TOOL_BUDGET：超限工具结果落盘
            if estimate_messages_tokens(messages) > COMPACT_THRESHOLD_TOKENS:   # ② FULL压缩
                before = len(messages)
            if estimate_messages_tokens(messages) > COMPACT_THRESHOLD_TOKENS:
                before = len(messages)
                new_msgs = compact_messages(self.llm, messages)
                if session is not None:
                    session.messages[:] = new_msgs      # 原地替换工作账本
                    session.append_transcript(          # 流水账只记事件，绝不被压缩
                        "compaction", detail=f"{before}条消息压缩为{len(new_msgs)}条")
                    messages = session.get()
                else:
                    messages = new_msgs
                if on_tool:
                    on_tool("🧹 上下文压缩", f"{before}条 → {len(new_msgs)}条")

            # ── 相位推导（纯函数，每步重算）─────────────────────
            phase, guidance = derive_phase(step, self.max_steps)
            if phase != last_phase:
                note = (f"[运行时提示] 当前相位: {phase}. {guidance} "
                        f"(第{step}步, 剩余{self.max_steps - step}步)")
                messages.append({"role": "user", "content": note})
                if self.on_phase:
                    self.on_phase(phase, step)
                last_phase = phase

            msg = self.llm.chat(messages, tools=schemas)

            if not msg.tool_calls:            # 模型不再要工具 = 说完了
                _remember(msg)                # 最终答案也进会话，下一轮才知道说过什么
                if session is not None:
                    session.append_transcript("assistant", text=msg.content)
                return msg.content

            _remember(msg)                    # assistant声明和tool结果必须配对

            for tc in msg.tool_calls:
                try:
                    tool_obj = self.tools.get(tc.function.name)
                    args = json.loads(tc.function.arguments)
                    result = tool_obj(**args)
                except Exception as e:
                    result = f"工具执行出错: {e}"   # 错误不炸循环，回传给模型自行纠正

                result = str(result)
                if on_tool:
                    on_tool(f"{tc.function.name}({tc.function.arguments[:80]})",
                            result[:100])

                if session is not None:
                    # 流水账记全文（messages里的可能已被TOOL_BUDGET换成预览）
                    session.append_transcript(
                        "tool", name=tc.function.name,
                        args=tc.function.arguments, result=result)

                _remember({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        return "达到最大步数限制（50步），任务未完成。"
