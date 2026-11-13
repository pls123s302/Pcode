"""Skill 体系：SKILL.md 内容包的发现/加载/菜单渲染（复刻 MiniCode skills.py，裁剪兼容层）

核心设计 = 两级渐进披露（Token成本与skill数量解耦）：
- 菜单常驻：每个 skill 只有 名字+一行描述 进 system 契约
- 全文按需：模型判断任务匹配 → 调 load_skill 工具 → 才加载 SKILL.md 全文

Skill 本质 = 一篇 markdown 操作手册：没有代码、没有注册、没有编译。
"路由/精排/意图识别"全部由模型读菜单自己完成——框架只负责把菜单端上来。

双根目录（同名时项目级优先）：
- 项目级  <工作区>/.pcode/skills/   （团队共享，可进git）
- 用户级  ~/.pcode/skills/          （个人库）
"""
import re
from pathlib import Path

from pcode.tool import tool
from pcode.memory_bank import adaptive_top_k   # 压力自适应剂量（与记忆块同一条斜坡）

USER_SKILLS_ROOT = Path.home() / ".pcode" / "skills"   # 测试可改道


def extract_description(markdown: str) -> str:
    """描述 = 正文第一个非标题段落的首行（MiniCode 同款规则）"""
    normalized = markdown.replace("\r\n", "\n")
    for block in (b.strip() for b in normalized.split("\n\n")):
        if not block or block.startswith("#"):
            continue
        for line in (p.strip() for p in block.split("\n")):
            if line and not line.startswith("#"):
                return line.replace("`", "")
    return "（无描述）"


def _skill_roots(cwd: str) -> list:
    return [
        (Path(cwd) / ".pcode" / "skills", "project"),
        (USER_SKILLS_ROOT, "user"),
    ]


def _scan_root(root: Path, source: str) -> list:
    """扫描一个根目录：每个含 SKILL.md 的子目录 = 一个技能"""
    found = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue                      # 坏符号链接/不可信挂载点，跳过
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        found.append({
            "name": entry.name,
            "description": extract_description(content),
            "path": str(skill_file),
            "source": source,
            "content": content,
        })
    return found


def discover_skills(cwd: str) -> list:
    """发现全部技能（含全文）。同名先到先得——项目级排前面所以压过用户级"""
    by_name = {}
    for root, source in _skill_roots(cwd):
        for skill in _scan_root(root, source):
            by_name.setdefault(skill["name"], skill)
    return list(by_name.values())


def get_skill(cwd: str, name: str) -> dict | None:
    """按名称取单个技能全文；不存在返回 None"""
    name = (name or "").strip()
    if not name:
        return None
    for root, _ in _skill_roots(cwd):
        f = root / name / "SKILL.md"
        if f.exists():
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                return None
            return {"name": name, "description": extract_description(content),
                    "path": str(f), "content": content}
    return None


def render_skill_menu(skills: list) -> str:
    """技能菜单：拼进 system 契约的动态后缀。
    幂等——skill集不变则渲染结果一字不变（前缀缓存照常命中）"""
    if not skills:
        return ""
    lines = ["## 可用 Skills（技能库）"]
    for s in skills:
        lines.append(f"- {s['name']}: {s['description']}")
    lines.append("用户点名某技能、或任务明显匹配某技能描述时，"
                 "先调用 load_skill 工具加载完整内容，再按其步骤执行。")
    return "\n".join(lines)


def create_load_skill_tool(workspace: str):
    """元工具：模型主动加载技能全文。
    它不作用于外部世界，而是作用于 agent 自己的知识——工具概念的升维。"""
    @tool
    def load_skill(name: str) -> str:
        """按名称加载一个 Skill 的完整内容（SKILL.md）。用户点名技能或任务匹配技能描述时，先加载再按步骤执行。"""
        skill = get_skill(workspace, name)
        if skill is None:
            names = ", ".join(s["name"] for s in discover_skills(workspace)) or "（当前无任何技能）"
            return f"错误: 未找到技能 {name}。可用技能: {names}"
        return f"SKILL: {skill['name']}\nPATH: {skill['path']}\n\n{skill['content']}"
    return load_skill


# ── 检索模式（技能数 > MENU_MAX 时接管路由）────────────────────
# 菜单模式（≤MENU_MAX）：全量菜单进system，模型自己扫——检索由"人肉扫描"完成
# 检索模式（>MENU_MAX）：菜单退位，一阶段=检索器召回top-k进尾部块，
#                        二阶段=load_skill精读；漏召兜底=find_skill自查
MENU_MAX = 30           # 菜单模式容量上限（"200"拐点的工程化落地）
SKILL_TOP_K = 3         # 每次召回的候选数
SKILL_GATE = 0.25       # 相关度门槛（低于此不进注入块）
# 高压拒注已升级：由 memory_bank.adaptive_top_k 的斜坡接管（<0.4 全额 → ≥0.75 关门）

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize(text: str) -> set:
    """英文按词、中文按二元字组（与memory_bank同款思路，独立实现——各自进化）"""
    words = {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}
    for run in _HAN_RE.findall(text):
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _overlap(query: str, doc: str) -> float:
    q = _tokenize(query)
    if not q:
        return 0.0
    return len(q & _tokenize(doc)) / len(q)


def is_retrieval_mode(cwd: str) -> bool:
    """regime 判定：技能数超过菜单容量即切检索模式"""
    return len(discover_skills(cwd)) > MENU_MAX


def render_retrieval_hint(count: int) -> str:
    """检索模式下进 system 契约的一行静态提示（替代全量菜单）"""
    return (f"技能库共{count}个技能，覆盖常见流程。需要特定领域流程时，"
            f"用 find_skill 工具搜索技能库；任务注入的<相关技能>块命中时优先照做。")


def search_skills(cwd: str, query: str, top_k: int = SKILL_TOP_K,
                  gate: float = SKILL_GATE) -> list:
    """一阶段召回：按任务文本给技能描述打分，取 top-k 且过门槛。
    描述是索引文档——它的质量决定召回率（检索模式的最高纪律）。"""
    query = (query or "").strip()
    if not query:
        return []
    scored = [(_overlap(query, s["description"] + " " + s["name"]), s)
              for s in discover_skills(cwd)]
    scored.sort(key=lambda x: -x[0])
    return [s for sc, s in scored[:top_k] if sc >= gate]


def injection_block(cwd: str, task: str, context_usage: float) -> str:
    """<相关技能>块（镜像 memory_bank.injection_block）：剂量随压力自适应收缩、无命中不注"""
    top_k = adaptive_top_k(context_usage)
    if top_k <= 0:
        return ""
    hits = search_skills(cwd, task, top_k=top_k)
    if not hits:
        return ""
    lines = ["<相关技能>"]
    for s in hits:
        lines.append(f"- {s['name']}: {s['description'][:120]}")
    lines.append("可用 find_skill 搜索更多技能；load_skill 加载完整步骤。")
    lines.append("</相关技能>")
    return "\n".join(lines)


def create_find_skill_tool(workspace: str):
    """元工具：漏召的兜底——任务开始时检索器没认出来，模型可以换词重查技能库"""
    @tool
    def find_skill(query: str) -> str:
        """在技能库中搜索相关技能。任务可能匹配某个技能但未被告知时，用它查询（可换关键词重试）。"""
        hits = search_skills(workspace, query, top_k=5)
        if not hits:
            total = len(discover_skills(workspace))
            return f"技能库（共{total}个技能）中没有与「{query}」相关的技能，可换关键词重试。"
        return "\n".join(f"- {h['name']}: {h['description'][:150]}" for h in hits)
    return find_skill
