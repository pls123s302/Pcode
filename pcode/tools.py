"""Pcode 内置工具：6 个，全部在真实文件系统上工作。
（run_command 的危险命令审批门禁计划在 V0.3 加入）
"""
import os
import subprocess

from pcode.tool import tool, ToolCollection

WORKSPACE = os.getcwd()   # 启动目录 = 工作区边界

# 树状展示时跳过的噪声目录/后缀
TREE_IGNORE = {"__pycache__", ".git", ".idea", ".venv", "node_modules"}


def _resolve(path: str) -> str:
    """把相对路径锚定到工作区，并拒绝越界访问。
    白名单例外：TOOL_BUDGET 落盘的缓存目录允许回读（指针承诺过可读）。"""
    root = os.path.abspath(WORKSPACE)
    p = os.path.abspath(os.path.join(root, path))
    if p.startswith(root):
        return p
    cache_root = str(CACHE_ROOT.resolve())
    if p.startswith(cache_root):
        return p
    raise PermissionError(f"路径逃出工作区: {path}")


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


@tool
def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    """分页读取文件内容（带行号）。offset=起始行号(从1开始), limit=最多读多少行。
    若文件被截断，末尾会提示用 offset 续读。"""
    p = _resolve(path)
    with open(p, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    if not lines:
        return "（空文件）"

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
    p = _resolve(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


@tool
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """精确替换：把文件中 old_string 唯一一次出现替换为 new_string"""
    p = _resolve(path)
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


@tool
def run_command(command: str) -> str:
    """在工作区目录执行一条 shell 命令（如运行测试）。超时60秒。
    注意：本工具返回全文；超大结果的落盘由统一出口 tool_budget_sweep 处理。"""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            timeout=60, cwd=WORKSPACE,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（超过60秒）被终止: {command}"

    output = (result.stdout or "") + (result.stderr or "")
    return f"退出码: {result.returncode}\n" + (output or "（无输出）")


@tool
def file_tree(path: str = ".", max_depth: int = 3) -> str:
    """树状展示目录结构，自动跳过 __pycache__/.git 等噪声目录"""
    root = _resolve(path)
    name = os.path.basename(root) or root
    lines = [f"{name}/"] + _tree(root, max_depth=max_depth)
    return "\n".join(lines)


def create_tool_collection() -> ToolCollection:
    return ToolCollection().add(
        list_files, read_file, write_file, edit_file, run_command, file_tree
    )
