"""~/.pcode 路径布局：数据按项目分区，缓存平铺。

单一事实源：记忆库（项目级 memory/）与会话（sessions/）都从这里推导路径，
保证"一个项目 = ~/.pcode/projects/<工作区键>/ 下的一个目录"。
布局：
    ~/.pcode/
    ├── memory/                  用户级记忆（跨项目通用）
    ├── cache/<会话id>/           工具输出存档（可丢弃，按会话 GC 整删）
    └── projects/<工作区键>/
        ├── memory/              项目级记忆
        └── sessions/            项目会话（<id>.json + deltas/<id>/）

测试改道：重定向 PROJECTS_ROOT 模块属性即可——project_dir() 在调用时
动态读取模块全局，import 时绑定的旧值不会残留。
"""
import re
import uuid
from pathlib import Path

PCODE_HOME = Path.home() / ".pcode"
PROJECTS_ROOT = PCODE_HOME / "projects"     # 项目数据分区根（记忆+会话的共同父目录）


def workspace_key(workspace: str) -> str:
    """工作区 → 稳定目录键：可读 slug + 路径指纹。
    同名项目在不同路径不撞键（uuid5 路径指纹）；同一项目不同路径拼法归一（resolve）"""
    p = Path(workspace).resolve()
    slug = re.sub(r"[^\w\-]", "_", p.name)[:40] or "default"
    return f"{slug}-{uuid.uuid5(uuid.NAMESPACE_URL, str(p)).hex[:8]}"


def project_dir(workspace: str) -> Path:
    """项目数据根：~/.pcode/projects/<工作区键>/（memory/ 与 sessions/ 的父目录）"""
    return PROJECTS_ROOT / workspace_key(workspace)
