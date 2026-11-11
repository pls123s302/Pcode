# -*- coding: utf-8 -*-
"""Pcode 记忆系统离线验证：反思四网/闸门/检索/注入/反馈/策展/集成
全程假LLM零API；落盘全在临时目录，不碰 ~/.pcode"""
import sys
import tempfile
from pathlib import Path

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = Path(tempfile.mkdtemp(prefix="pcode_mem_test_"))
import pcode.session as ps
ps.SESSIONS_DIR = ROOT / "sessions"

from pcode.memory_bank import MemoryBank, STALE_MARK
from pcode.reflection import reflect
from pcode.agent import Agent
from pcode.session import Session

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

# ── 假件 ──────────────────────────────────────────────────────
class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class FakeFn:
    def __init__(self, name, args="{}"):
        self.name = name
        self.arguments = args

class FakeTC:
    def __init__(self, tc_id, name, args="{}"):
        self.id = tc_id
        self.function = FakeFn(name, args)

class FakeTools:
    def to_openai_schemas(self):
        return []
    def get(self, name):
        return lambda **kw: f"{name} 完成，没有报错"

class ScriptLLM:
    """第1次成功调用→发起工具；之后→收工。洞见调用(含'提炼')→固定回复"""
    def __init__(self):
        self.served = 0
    def chat(self, messages, tools=None):
        text = str(messages[0].get("content") or "")
        if "提炼" in text and "记忆" in text:
            return FakeMsg(content="本机改代码的固定套路是先读目标文件，再用edit_file修改，最后用run_command验证结果是否真正生效。")
        self.served += 1
        if self.served % 2 == 1:      # 每个任务都走"发工具→收工"两步
            return FakeMsg(tool_calls=[FakeTC("c1", "run_command", '{"path": "tools.py"}')])
        return FakeMsg(content="任务完成")

print("== 一、reflect：四张网 ==")
# A) 犯错但最终修复（transcript形态）→ 黄金记忆应过闸
slice_a = [
    {"kind": "user", "text": "修复 settings.json 的 BOM 问题"},
    {"kind": "tool", "name": "edit_file",
     "args": '{"path": "settings.json"}', "result": "工具执行出错: UnicodeEncodeError ascii"},
    {"kind": "assistant", "text": "看来是编码问题，决定改用 WriteAllText 方案"},
    {"kind": "tool", "name": "run_command", "args": '{"command": "python fix.py"}',
     "result": "修复完成"},
    {"kind": "assistant", "text": "修好了。PowerShell 写 utf8 会带 BOM，要用 UTF8Encoding(false)"},
]
check("A0: MiniCode式推断(不知出口)会错杀黄金记忆——所以必须传出口",
      reflect("修复 settings.json 的 BOM 问题", slice_a) is None)
r = reflect("修复 settings.json 的 BOM 问题", slice_a, success=True)
check("A1: 传入出口成败→过闸产出条目", r is not None)
check("A2: 犯错但成功→confidence≥0.5", r and r["success"] and r["confidence"] >= 0.5)
check("A3: 事实网捞到路径→task_context", r and r["category"] == "task_context"
      and "settings.json" in r["content"])
check("A4: 决策网捞到转折", r and "决定改用 WriteAllText" in r["content"])
check("A5: 错误网捞到BOM", r and "Errors Encountered" in r["content"])
check("A6: 工具入tags", r and "run_command" in r["tags"])

# B) 纯失败（有错没修好）→ 数学上过不了闸
slice_b = [
    {"kind": "tool", "name": "run_command", "args": "{}", "result": "工具执行出错: timeout"},
    {"kind": "tool", "name": "run_command", "args": "{}", "result": "工具执行出错: timeout"},
    {"kind": "assistant", "text": "还是失败，我决定放弃这个方案"},
]
check("B: 纯失败被闸门拦下(没有解法的坑不存)",
      reflect("跑测试", slice_b, success=False) is None)

# C) 纯寒暄 → 初筛就拦截
check("C: 纯寒暄初筛拦截", reflect("你好", [{"kind": "user", "text": "你好"}]) is None)

# D) messages形态（无会话路径）也能兜住
slice_d = [
    {"role": "system", "content": "你是Pcode"},
    {"role": "user", "content": "改配置"},
    {"role": "assistant", "content": "我决定采用直接编辑方案"},
    {"role": "tool", "tool_call_id": "x", "content": "编辑完成"},
]
r_d = reflect("改配置", slice_d, success=True)
check("D: messages形态可处理", r_d is not None and r_d["success"] is True)

# E) 纯问答误捞 → reflection类目
slice_e = [
    {"kind": "assistant", "text": "GIL 使用互斥锁实现，保证同一时刻只有一个线程执行字节码"},
]
r_e = reflect("GIL是什么", slice_e)
check("E: ③号筐空→reflection类目", r_e is not None and r_e["category"] == "reflection")

print("== 二、MemoryBank：存储/检索/注入/反馈 ==")
bank = MemoryBank(root=ROOT / "bank")
bank.add_entry("task_context", "修复 settings.json 的 BOM 编码问题，用 WriteAllText 解决",
               ["self-reflection", "success"])
check("存储: json+md双文件落盘", (ROOT / "bank" / "memory.json").exists()
      and (ROOT / "bank" / "MEMORY.md").exists())
check("投影md含类目标题", "任务事实" in (ROOT / "bank" / "MEMORY.md").read_text(encoding="utf-8"))
bank.add_entry("reflection", "本机跑测试要先 conda activate Langgraph 再 pytest", ["success"])
check("重开实例读回事实源", len(MemoryBank(root=ROOT / "bank").entries) == 2)

hits = bank.search("settings.json 又出 BOM 编码错误了怎么办")
check("检索: 相关条目命中且排前", bool(hits) and "settings.json" in hits[0]["content"])
check("检索: 不相关任务给不出结果", bank.search("帮我写一个贪吃蛇游戏") == [])

block, ids = bank.injection_block("settings.json 又出 BOM 编码错误了怎么办", 0.1)
check("注入: 标记块格式正确", block.startswith("<记忆上下文>")
      and block.endswith("</记忆上下文>") and "[task_context]" in block)
check("注入: 返回追责id列表", len(ids) == 1)
check("注入: 高压拒注(≥0.75)", bank.injection_block("settings.json BOM", 0.8) == ("", []))

bank.feedback(True, ids)
check("反馈: 成功+2", bank.entries[0]["usage_count"] == 2)
bank.feedback(False, ids)
check("反馈: 失败−1(不为负)", bank.entries[0]["usage_count"] == 1)

print("== 三、策展人 ==")
bank2 = MemoryBank(root=ROOT / "bank2", workspace=str(ROOT / "ws"))
(ROOT / "ws").mkdir(parents=True, exist_ok=True)
(ROOT / "ws" / "alive.py").write_text("x", encoding="utf-8")
for i in range(9):
    bank2.maintain()                       # 攒到第10次
check("周期: 前9次不触发", bank2.task_count == 9)
check("计数落盘: 中途重开实例不丢(无产出任务也计数)",
      MemoryBank(root=ROOT / "bank2").task_count == 9)
bank2.add_entry("reflection", "处理 alive.py 要注意 yaml 库的缩进陷阱，用 xyztool 的 safe_load", ["success"])
bank2.add_entry("reflection", "处理 alive.py 要注意 yaml 库的缩进陷阱，用 xyztool 的 safe_load 读取", ["success"])
bank2.add_entry("reflection", "yaml 缩进陷阱：xyztool 的 safe_load 处理 alive.py", ["success"])
bank2.add_entry("task_context", "改过 ghost_file.py 的编码", ["success"])
rpt = bank2.maintain(llm=ScriptLLM())
check("周期: 第10次触发并返回报告", rpt is not None)
check("策展: 近重复去重(留长的删短的)", rpt["dedup"] >= 1)
check("策展: 验尸给幽灵文件打过时标记", rpt["stale"] == 1
      and any(e["content"].startswith(STALE_MARK) for e in bank2.entries))
check("策展: LLM熬出insight", rpt["insights"] == 1
      and any(e["category"] == "insight" for e in bank2.entries))
check("策展: 过时条目绝不出现在注入结果里",
      all(not e["content"].startswith(STALE_MARK)
          for e in bank2.search("ghost_file.py 的编码改动")))

print("== 四、Agent 集成（注入→干活→沉淀→反馈全链）==")
bank3 = MemoryBank(root=ROOT / "bank3")
sess = Session(workspace=str(ROOT))
agent = Agent(llm=ScriptLLM(), tools=FakeTools(), memory_bank=bank3, max_steps=10)
answer1 = agent.run("修复 tools.py 里的 BOM 编码问题", session=sess, on_tool=lambda d, p: None)
check("集成: 任务完成且记忆库长出第一条", answer1 == "任务完成" and len(bank3.entries) == 1)
check("集成: 反思归类task_context(参数里有路径)", bank3.entries[0]["category"] == "task_context")
check("集成: system契约在位置0", sess.messages[0]["role"] == "system")
first_len = len(sess.messages)

answer2 = agent.run("再修一次 tools.py 的 BOM 编码问题", session=sess, on_tool=lambda d, p: None)
task_msgs = [m for m in sess.messages if m.get("role") == "user"
             and not str(m.get("content", "")).startswith("[运行时提示]")]
check("集成: 第一任务未被注入(库当时是空的)", not task_msgs[0]["content"].startswith("<记忆上下文>"))
check("集成: 第二任务尾部注入记忆块", task_msgs[-1]["content"].startswith("<记忆上下文>"))
check("集成: 注入的是上一次沉淀的那条", len(agent._injected_ids) == 1)
check("集成: 成功反馈+2", bank3.entries[0]["usage_count"] == 2)
check("集成: system头未重复", sum(1 for m in sess.messages if m["role"] == "system") == 1)
check("集成: 会话正常增长(含2条相位提示，注入是改内容不是加消息)",
      len(sess.messages) == first_len + 6)

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项"
      + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
