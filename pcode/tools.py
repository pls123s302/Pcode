"""Pcode 内置工具：核心层 6 个常驻原子工具 + 扩展层（tools_ext，profile=full 懒加载）。
（危险命令审批门禁 V0.3 兑现：见 pcode/permissions.py 的四层审查链路）
"""
import os
import subprocess
from pathlib import Path

from pcode.memory import CACHE_ROOT   # sweep 档案根：读回白名单的判定依据
from pcode.tool import tool, ToolCollection

WORKSPACE = os.getcwd()   # 启动目录 = 工作区边界
PCODE_HOME = str(Path.home() / ".pcode")   # 自我修改边界：Agent 的记忆/技能/会话/缓存
# （模块级变量而非函数内取值——测试可改道临时目录）

# 树状展示时跳过的噪声目录/后缀
TREE_IGNORE = {"__pycache__", ".git", ".idea", ".venv", "node_modules"}


def _resolve(path: str, for_write: bool = False) -> str:
    """把相对路径锚定到工作区，并拒绝越界访问。
    白名单例外（只读）：TOOL_BUDGET 落盘的缓存目录允许回读（指针承诺过可读）。
    写入边界（工具自检层）：~/.pcode/ 整体拒绝写入——记忆/技能/会话/缓存是
    Agent 的"自我"，工具改它们 = 越权修改自己的能力边界（审查链路第2层）。"""
    root = os.path.abspath(WORKSPACE)
    p = os.path.abspath(os.path.join(root, path))
    if not (p.startswith(root) or p.startswith(str(CACHE_ROOT.resolve()))):
        raise PermissionError(f"路径逃出工作区: {path}")
    if for_write and p.startswith(PCODE_HOME):
        raise PermissionError(
            f"自我修改边界: {path} 位于 Pcode 数据目录（~/.pcode），"
            f"工具不允许写入——记忆/技能/会话由框架自己管理")
    return p


def _tree(dir_path: str, prefix: str = "", depth: int = 0, max_depth: int = 3) -> list:
    """递归生成树状行。prefix 是本层继承的缩进前缀"""
    if depth >= max_depth:
        return [f"{prefix}└── ...(已达最大深度 {max_depth})"]

    entries = sorted(
        os.listdir(dir_path),
        key=lambda n: (not os.path.isdir(os.path.join(dir_path, n)), n.lower()),
    )
    entries = [
        e for e in entries
        if e not in TREE_IGNORE and not e.endswith(".egg-info")
    ]
    if not entries:
        return [f"{prefix}└── (空)"]

    lines = []
    for i, name in enumerate(entries):
        full = os.path.join(dir_path, name)
        is_last = (i == len(entries) - 1)
        connector = "└── " if is_last else "├── "
        if os.path.isdir(full):
            lines.append(f"{prefix}{connector}{name}/")
            child_prefix = prefix + ("    " if is_last else "│   ")
            lines.extend(_tree(full, child_prefix, depth + 1, max_depth))
        else:
            lines.append(f"{prefix}{connector}{name}")
    return lines


@tool
def list_files(path: str = ".") -> str:
    """列出工作区内某个目录下的文件和子目录"""
    target = _resolve(path)
    entries = sorted(os.listdir(target))
    if not entries:
        return f"{path} 是空目录"
    return "\n".join(
        f"{'[目录]' if os.path.isdir(os.path.join(target, n)) else '[文件]'} {n}"
        for n in entries
    )


MAX_LINE_CHARS = 2000   # 单行截断：防minified单行巨文件（新鲜豁免+压缩保留区都拦不住它）


@tool
def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    """分页读取文件内容（带行号）。offset=起始行号(从1开始), limit=最多读多少行。
    若文件被截断，末尾会提示用 offset 续读。"""
    p = _resolve(path)
    with open(p, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    if not lines:
        return "（空文件）"

    # 单行截断：一行超2000字的部分切掉并标记（minified压缩文件可能单行几MB）
    lines = [
        ln[:MAX_LINE_CHARS] + f"...[本行超长，已截断，原长{len(ln)}字]" if len(ln) > MAX_LINE_CHARS
        else ln
        for ln in lines
    ]

    offset = max(1, offset)
    limit = max(1, min(limit, 1000))              # 单次上限1000行，防一次吃成胖子
    chunk = lines[offset - 1: offset - 1 + limit]

    numbered = "\n".join(
        f"{offset + i:4d}| {line}" for i, line in enumerate(chunk)
    )
    result = numbered or f"（第{offset}行起没有内容，文件共{len(lines)}行）"

    end = offset - 1 + len(chunk)
    if end < len(lines):
        result += f"\n\n[TRUNCATED] 文件共{len(lines)}行，当前读到第{end}行。" \
                  f"用 offset={end + 1} 继续读下一段。"
    return result


@tool
def write_file(path: str, content: str) -> str:
    """创建新文件，或整体重写一个已有文件"""
    p = _resolve(path, for_write=True)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


@tool
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """精确替换：把文件中 old_string 唯一一次出现替换为 new_string"""
    p = _resolve(path, for_write=True)
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_string)
    if count == 0:
        return f"错误: 在 {path} 中找不到要替换的内容，请先用 read_file 确认原文。"
    if count > 1:
        return f"错误: 匹配到 {count} 处，请提供包含更多上下文的 old_string 使其唯一。"

    with open(p, "w", encoding="utf-8") as f:
        f.write(content.replace(old_string, new_string, 1))
    return f"已修改 {path}"


MAX_OUTPUT_CHARS = 30_000   # 源头硬上限：单条结果的最大"安全剂量"
# （新鲜豁免会让sweep放过刚产出的结果，所以源头必须自带上限——学MiniCode MAX_OUTPUT_CHARS）


@tool
def run_command(command: str) -> str:
    """在工作区目录执行一条 shell 命令（如运行测试）。超时60秒。
    危险命令会转人工审批（规则过滤→y/a/n 确认），被拒绝时把原因当反馈换方案。"""
    from pcode import permissions      # 懒导入：无危险命令的场景不付这张 import
    permissions.ensure_command(command)   # 第1层规则过滤 + 第4层人工确认（fail-closed）
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            timeout=60, cwd=WORKSPACE,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（超过60秒）被终止: {command}"

    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = (output[:MAX_OUTPUT_CHARS]
                  + f"\n\n[输出超长，已截断至 {MAX_OUTPUT_CHARS} 字符。"
                    f"建议用更精确的命令（加过滤/限定范围）缩小输出后重跑]")
    return f"退出码: {result.returncode}\n" + (output or "（无输出）")


@tool
def file_tree(path: str = ".", max_depth: int = 3) -> str:
    """树状展示目录结构，自动跳过 __pycache__/.git 等噪声目录"""
    root = _resolve(path)
    name = os.path.basename(root) or root
    lines = [f"{name}/"] + _tree(root, max_depth=max_depth)
    return "\n".join(lines)


# ── 工具面分层（profile 机制，照抄MiniCode toolProfile）──────────
# 哲学：上下文是配给制——工具的 schema 每次请求都全量在场，
# 工具面越大，token成本越高、模型选错工具的概率越大。
# core 档只给模型看常驻6件；full 档附加扩展层（懒导入，连requests都不加载）。

_CORE_TOOLS = [list_files, read_file, write_file, edit_file, run_command, file_tree]
_FULL_ALIASES = {"full", "all", "utility", "utilities"}


def create_tool_collection_profiled(profile: str = "core",
                                    workspace: str | None = None) -> ToolCollection:
    """profile: core（默认）= 6个常驻原子工具；full/all/utility = 附加扩展层
    workspace: 提供则注册元工具（不受profile管——MiniCode同款）：
      load_skill 永远注册；find_skill 仅检索模式注册（菜单模式下模型看得见全量菜单）"""
    collection = ToolCollection().add(*_CORE_TOOLS)
    if str(profile).strip().lower() in _FULL_ALIASES:
        from pcode import tools_ext          # 懒导入：core档连requests的import都不付
        collection.add(*tools_ext.EXTENSION_TOOLS)
    if workspace:
        from pcode.skills import (create_load_skill_tool, create_find_skill_tool,
                                  is_retrieval_mode)
        collection.add(create_load_skill_tool(workspace))
        if is_retrieval_mode(workspace):
            collection.add(create_find_skill_tool(workspace))
    return collection
