# -*- coding: utf-8 -*-
"""Pcode V0.4 离线验证：撞墙抢救(REACTIVE) + 每步存盘 + 断点悬空修复
全程假LLM，不花一分钱API；落盘改道临时目录，不污染 ~/.pcode"""
import sys
import tempfile
from pathlib import Path

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = Path(tempfile.mkdtemp(prefix="pcode_test_"))
import pcode.memory as pm
import pcode.paths as pp
import pcode.session as ps

pm.CACHE_ROOT = ROOT / "cache"
pp.PROJECTS_ROOT = ROOT / "projects"     # 会话分区根改道临时目录

from pcode.agent import Agent, _is_context_overflow
from pcode.session import Session, load_session

# ── 假件 ──────────────────────────────────────────────────────
class FakeOverflow(Exception):
    def __init__(self, msg="Error code: 400 - maximum context length is 8 tokens"):
        super().__init__(msg)
        self.status_code = 400

class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class FakeFn:
    def __init__(self, name, args="{}"):
        self.name = name
        self.arguments = args

class FakeTC:
    def __init__(self, tc_id, name):
        self.id = tc_id
        self.function = FakeFn(name)

class FakeTools:
    def to_openai_schemas(self):
        return []
    def get(self, name):
        return lambda **kw: "工具结果" * 300      # 1200字符，不触发sweep(2000线)

WALL = 8   # 假想小窗口模型：消息超过8条就撞墙

class WallLLM:
    """正常干活（第一次成功的调用先调工具，之后收工）；消息数超墙就抛400超限"""
    def __init__(self, poison=None, wall=WALL):
        self.calls = 0
        self.served = 0                 # 成功返回（没抛异常）的次数
        self.hit_wall = False
        self.poison = poison            # 第2次调用改抛的异常（测"非超限400不乱救"）
        self.wall = wall                # 每个场景可以有自己的墙高
    def chat(self, messages, tools=None):
        self.calls += 1
        if len(messages) == 1 and "压缩成一段中文摘要" in str(messages[0].get("content")):
            return FakeMsg(content="（摘要）用户在做Pcode测试任务")
        if self.calls == 2 and self.poison is not None:
            raise self.poison
        if len(messages) > self.wall:
            self.hit_wall = True
            raise FakeOverflow()
        self.served += 1
        if self.served % 2 == 1:
            return FakeMsg(tool_calls=[FakeTC("call_1", "dummy")])
        return FakeMsg(content="任务完成")

class GiantLLM:
    """无论怎么压都超限——模拟'单条巨物'，抢救注定失败"""
    def __init__(self):
        self.calls = 0
    def chat(self, messages, tools=None):
        self.calls += 1
        if len(messages) == 1 and "压缩成一段中文摘要" in str(messages[0].get("content")):
            return FakeMsg(content="（摘要）")
        raise FakeOverflow()

def on_tool(desc, preview):
    print(f"    [on_tool] {desc} → {preview}")

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

print("== 单元：_is_context_overflow ==")
check("400+context length → 是超限", _is_context_overflow(FakeOverflow()))
check("DashScope措辞 input length → 是超限",
      _is_context_overflow(FakeOverflow("Error code: 400 - Range of input length should be [1, 30720]")))
check("普通400（无效key）→ 不误判", not _is_context_overflow(Exception("Error code: 400 - invalid api key")))
e429 = FakeOverflow("429 too many requests"); e429.status_code = 429
check("429 → 不是超限", not _is_context_overflow(e429))

print("== 场景A：撞墙 → 抢救成功 ==")
s = Session(workspace=str(ROOT))
for i in range(9):   # 臃肿历史：9条胖assistant（sweep不管assistant，体积留着）
    s.append({"role": "assistant", "content": f"历史第{i}条。" + "细节" * 2000})
llm = WallLLM()
answer = Agent(llm=llm, tools=FakeTools()).run("继续任务", session=s, on_tool=on_tool)
check("撞墙被识别并抢救成功", llm.hit_wall and answer == "任务完成")
check("抢救压缩确实发生（消息变少）", len(s.messages) < 10)
details = [e.get("detail", "") for e in s.transcript if e["kind"] == "compaction"]
check("流水账记录了抢救事件", any("撞墙抢救" in d for d in details))
check("抢救后仍能继续干活（工具轮正常）", any(e["kind"] == "tool" for e in s.transcript))

print("== 场景B：非超限400 → 原样抛出 + 每步存盘保住历史 ==")
s2 = Session(workspace=str(ROOT))
llm2 = WallLLM(poison=Exception("Error code: 400 - invalid api key"))
crashed = False
try:
    Agent(llm=llm2, tools=FakeTools()).run("小任务", session=s2, on_tool=on_tool)
except Exception:
    crashed = True
check("非超限400不被误救，原样抛出", crashed)
s2.save()
loaded = load_session(s2.session_id)
check("崩溃后磁盘上仍有完整历史（异常路径边界同步兜底）",
      loaded is not None and len(loaded.messages) >= 3)
roles = [m["role"] for m in loaded.messages]
check("盘上assistant/tool成对无悬空", roles.count("assistant") == roles.count("tool"))

print("== 场景C：断点悬空修复 ==")
s3 = Session(workspace=str(ROOT))
s3.append({"role": "user", "content": "上次的任务"})
s3.append({"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_x", "type": "function",
     "function": {"name": "dummy", "arguments": "{}"}}]})
# ↑ 没有 tool 结果——上次崩溃留下的悬空现场
llm3 = WallLLM(wall=12)   # 场景C专注测修复，墙抬高避免中途触发抢救压缩吃掉占位结果
answer3 = Agent(llm=llm3, tools=FakeTools()).run("接着做", session=s3, on_tool=on_tool)
tools_msgs = [m for m in s3.messages if m["role"] == "tool"]
check("悬空调用被补齐（不再400卡死）",
      any(m.get("tool_call_id") == "call_x" for m in tools_msgs))
check("修复记录进流水账",
      any("断点修复" in e.get("detail", "") for e in s3.transcript))
check("修复后任务正常完成", answer3 == "任务完成")

print("== 场景D：单条巨物 → 只救一次，坦率放弃 ==")
llm4 = GiantLLM()
raised = False
try:
    Agent(llm=llm4, tools=FakeTools()).run("x", session=None, on_tool=on_tool)
except Exception:
    raised = True
check("救不动时坦率抛错（不无限重试）", raised and llm4.calls == 2)

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项" + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
