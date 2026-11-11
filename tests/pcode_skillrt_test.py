# -*- coding: utf-8 -*-
"""检索模式（破菜单容量后）离线验证：regime切换/检索器/注入块/find_skill/高压拒注
落盘改道临时目录，不碰真实 ~/.pcode"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="pcode_skillrt_test_"))
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pcode.skills as pskill
pskill.USER_SKILLS_ROOT = ROOT / "user_skills"

from pcode.skills import (MENU_MAX, discover_skills, is_retrieval_mode,
                          search_skills, injection_block,
                          render_skill_menu, render_retrieval_hint,
                          create_find_skill_tool, create_load_skill_tool)
from pcode.tools import create_tool_collection_profiled

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

def make_skill(base, name, desc):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n\n{desc}\n", encoding="utf-8")

WS = ROOT / "ws"
print("== 一、regime 切换（跨过 MENU_MAX 的那一刻）==")
make_skill(WS / ".pcode" / "skills", "seed", "初始化占位技能")
for i in range(MENU_MAX - 1):                    # 造到 30 个（1 seed + 29）
    make_skill(WS / ".pcode" / "skills", f"topic{i}", f"处理主题{i}领域的固定流程")
check(f"{MENU_MAX}个 → 菜单模式", not is_retrieval_mode(str(WS)))
make_skill(WS / ".pcode" / "skills", "api-debug", "调试 HTTP API 接口的认证与响应问题，含401排查流程")
check(f"{MENU_MAX+1}个 → 自动切检索模式", is_retrieval_mode(str(WS)))
check("检索模式下菜单提示替代全量菜单",
      render_retrieval_hint(len(discover_skills(str(WS)))).startswith("技能库共31个"))
check("菜单模式渲染函数仍可用（向下兼容）",
      "seed" in render_skill_menu(discover_skills(str(WS))))

print("== 二、检索器 ==")
hits = search_skills(str(WS), "调试 HTTP 接口 401 认证问题")
check("检索: 目标技能命中且排第一", hits and hits[0]["name"] == "api-debug")
check("检索: 门槛过滤噪声（无关查询空手而归）",
      search_skills(str(WS), "帮我做一杯奶茶") == [])

print("== 三、注入块 ==")
block = injection_block(str(WS), "调试 HTTP 接口 401 认证问题", 0.1)
check("注入: 标记块格式", block.startswith("<相关技能>") and block.endswith("</相关技能>"))
check("注入: 命中行在块内", "api-debug" in block)
check("注入: 高压拒注(≥0.75)", injection_block(str(WS), "调试 HTTP 接口", 0.8) == "")

print("== 四、find_skill 元工具（漏召兜底）==")
fs = create_find_skill_tool(str(WS))
r = fs(query="接口认证调试")
check("find_skill: 换词也能命中", "api-debug" in r)
r = fs(query="做一杯奶茶")
check("find_skill: 无结果给提示+总数", "没有与" in r and "31" in r)
ls = create_load_skill_tool(str(WS))
check("load_skill 与 find_skill 共存", "SKILL: api-debug" in ls(name="api-debug"))

print("== 五、注册规则（检索模式）==")
reg = create_tool_collection_profiled("core", workspace=str(WS))
check("检索模式: load_skill+find_skill都注册（core档）",
      {"load_skill", "find_skill"} <= set(reg.tools))
check("检索模式: 核心工具仍在", {"read_file", "run_command"} <= set(reg.tools))

print("== 六、agent 集成（检索模式注入进任务消息）==")
class M:
    def __init__(s, c=None): s.content = c; s.tool_calls = None
class FakeTools:
    def to_openai_schemas(s): return []
class L:
    def chat(s, m, tools=None): return M("任务完成")

import pcode.session as ps
ps.SESSIONS_DIR = ROOT / "sessions"
from pcode.session import Session
from pcode.agent import Agent
agent = Agent(llm=L(), tools=FakeTools(), skill_menu=render_retrieval_hint(31),
              skill_search=lambda task, cu: injection_block(str(WS), task, cu))
sess = Session(workspace=str(ROOT))
agent.run("调试 HTTP 接口 401 认证问题", session=sess)
tasks = [m for m in sess.messages if m.get("role") == "user"
         and not str(m.get("content", "")).startswith("[运行时提示]")]
check("集成: 任务消息携带<相关技能>块", tasks[0]["content"].startswith("<相关技能>"))
check("集成: system头是检索提示而非全量菜单",
      "技能库共31个" in sess.messages[0]["content"]
      and "- topic0:" not in sess.messages[0]["content"])

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项"
      + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
