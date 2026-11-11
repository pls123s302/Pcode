# -*- coding: utf-8 -*-
"""Skill 体系离线验证：发现/加载/菜单/元工具/agent集成
双根目录落盘改道临时目录，不碰真实 ~/.pcode"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="pcode_skill_test_"))
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pcode.skills as pskill
pskill.USER_SKILLS_ROOT = ROOT / "user_skills"       # 用户级改道
import pcode.session as ps
ps.SESSIONS_DIR = ROOT / "sessions"

from pcode.skills import (discover_skills, get_skill, extract_description,
                          render_skill_menu, create_load_skill_tool)
from pcode.tools import create_tool_collection_profiled
from pcode.agent import Agent
from pcode.session import Session

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

def make_skill(base, name, desc_line, body="\n正文步骤略。\n"):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n\n{desc_line}\n{body}", encoding="utf-8")

WS = ROOT / "ws"; WS.mkdir()
PROJ = WS / ".pcode" / "skills"
USER = ROOT / "user_skills"
make_skill(PROJ, "test-runner", "用 pytest 跑测试并修到全绿", "\n步骤: 先收集再执行。\n")
make_skill(PROJ, "commit-helper", "按约定式提交规范生成 commit message")
make_skill(USER, "bom-safe-write", "Windows 下写 utf8 文件避免 BOM 的固定姿势")
make_skill(USER, "test-runner", "（用户级旧版）跑测试")   # 与项目级同名

print("== 一、发现与优先级 ==")
skills = discover_skills(str(WS))
names = {s["name"] for s in skills}
check("发现3个技能（同名去重）", len(skills) == 3 and names == {"test-runner", "commit-helper", "bom-safe-write"})
tr = next(s for s in skills if s["name"] == "test-runner")
check("同名时项目级压过用户级", tr["source"] == "project" and "pytest" in tr["content"])

print("== 二、描述提取 ==")
check("描述=首个非标题段落首行", extract_description("# 标题\n\n用 pytest 跑测试并修到全绿\n") == "用 pytest 跑测试并修到全绿")
check("反引号被清理", extract_description("## t\n\n用 `pytest` 跑\n") == "用 pytest 跑")
check("无正文兜底", extract_description("# 只有标题\n") == "（无描述）")

print("== 三、get_skill / 元工具 ==")
s = get_skill(str(WS), "commit-helper")
check("get_skill返回全文", s is not None and "约定式提交" in s["content"])
check("未知技能返回None", get_skill(str(WS), "不存在") is None)

tool = create_load_skill_tool(str(WS))
r = tool(name="bom-safe-write")
check("元工具加载全文", "bom-safe-write" in r and "BOM" in r)
r = tool(name="不存在")
check("元工具报错并列出可用技能", r.startswith("错误") and "test-runner" in r and "bom-safe-write" in r)

print("== 四、菜单渲染 ==")
check("空技能集→空菜单", render_skill_menu([]) == "")
menu = render_skill_menu(skills)
check("菜单含全部技能行", "- test-runner: 用 pytest 跑测试并修到全绿" in menu
      and "- bom-safe-write:" in menu)
check("菜单自带使用指令", "load_skill" in menu)

print("== 五、注册与 agent 集成 ==")
core = create_tool_collection_profiled("core")                 # 无workspace → 无元工具
check("无workspace: 不注册元工具", "load_skill" not in core.tools)
withws = create_tool_collection_profiled("core", workspace=str(WS))
check("有workspace: core档也注册元工具（不受profile管）", "load_skill" in withws.tools)

class M:
    def __init__(s, c=None): s.content = c; s.tool_calls = None
class FakeTools:
    def to_openai_schemas(s): return []
class L:
    def chat(s, m, tools=None): return M("任务完成")

menu = render_skill_menu(discover_skills(str(WS)))
sess = Session(workspace=str(ROOT))
agent = Agent(llm=L(), tools=FakeTools(), skill_menu=menu)
answer = agent.run("跑一下测试", session=sess)
sys_head = sess.messages[0]
check("集成: system头含静态契约", "你是 Pcode" in sys_head["content"])
check("集成: system头含技能菜单", "## 可用 Skills" in sys_head["content"]
      and "- test-runner:" in sys_head["content"])
check("集成: 菜单在契约之后（静态在前动态在后）",
      sys_head["content"].index("你是 Pcode") < sys_head["content"].index("## 可用 Skills"))
agent.run("再跑一次", session=sess)
check("集成: 第二任务system头幂等（一字未变）", sess.messages[0]["content"] == sys_head["content"])

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项"
      + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
