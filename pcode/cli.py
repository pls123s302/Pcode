"""Pcode CLI — 终端界面"""
import argparse
import os
import time
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from pcode.agent import Agent
from pcode.config import load_settings, SETTINGS_PATH
from pcode.llm import LLM
from pcode.session import (Session, load_session, list_sessions,
                           get_latest_session)
from pcode.memory import set_session
from pcode.memory_bank import MemoryBank
from pcode.skills import (discover_skills, render_skill_menu,
                          render_retrieval_hint, injection_block,
                          is_retrieval_mode)
from pcode.tools import create_tool_collection_profiled
from pcode.permissions import PermissionManager, install as install_permission_gate

console = Console()

BANNER = r"""
██████╗  ██████╗ ██████╗ ██████╗ ███████╗
██╔══██╗██╔════╝██╔═══██╗██╔══██╗██╔════╝
██████╔╝██║     ██║   ██║██║  ██║█████╗
██╔═══╝ ██║     ██║   ██║██║  ██║██╔══╝
██║     ╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝      ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
   轻量级终端编码 Agent · v0.1
"""

HELP_TEXT = """\
## 命令
| 命令           | 作用                          |
|----------------|-------------------------------|
| /help          | 显示本帮助                    |
| /tools         | 查看我的工具箱                |
| /new           | 开一个全新会话                |
| /sessions      | 列出本工作区的历史会话        |
| /resume latest | 恢复本工作区最近的会话        |
| /resume <id>   | 恢复指定会话                  |
| /replay        | 回放本会话完整流水账          |
| /exit          | 退出 Pcode                    |

除命令外的一切输入都会作为任务交给 Pcode 执行。"""


def show_replay(session):
    """回放流水账（transcript永不压缩，所以再久远的细节都在）"""
    if not session.transcript:
        console.print("[dim]本会话还没有流水记录。[/dim]")
        return
    lines = []
    for e in session.transcript:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        kind = e["kind"]
        if kind == "user":
            body = e.get("text", "")[:60]
        elif kind == "assistant":
            body = e.get("text", "")[:60]
        elif kind == "tool":
            body = f"{e.get('name')} → {e.get('result', '')[:50]}"
        else:  # compaction 等事件
            body = e.get("detail", "")
        lines.append(f"[dim]{ts}[/dim] [magenta]{kind:<10}[/magenta] {body}")
    console.print(Panel(
        "\n".join(lines),
        title=f"流水账（{len(session.transcript)}条 · 完整未压缩）",
        border_style="magenta",
    ))


def build_agent(workspace: str) -> Agent:
    """装配大脑：读配置（没有就提醒用户去建）→ 模型 + 工具箱 + 记忆库 + 主循环"""
    cfg = load_settings()
    if not cfg["api_key"]:
        console.print(Panel(
            f"[red]还没有配置 API Key，Pcode 无法思考。[/red]\n\n"
            f"请创建配置文件（目录不存在就手动建）：\n"
            f"[cyan]{SETTINGS_PATH}[/cyan]\n\n"
            f"内容：\n"
            f'{{\n'
            f'  "api_key": "sk-你的Key",\n'
            f'  "model": "qwen-plus",          ← 可选，默认qwen-plus\n'
            f'  "base_url": "https://..."      ← 可选，默认DashScope\n'
            f'}}'
        ))
        raise SystemExit(1)

    console.print(f"[dim]模型: {cfg['model']} · 配置: {SETTINGS_PATH}[/dim]")
    llm = LLM(model=cfg["model"], api_key=cfg["api_key"], base_url=cfg["base_url"])
    # 工具面分档（settings.json 的 tool_profile / 环境变量 PCODE_TOOL_PROFILE）
    profile = os.environ.get("PCODE_TOOL_PROFILE") or cfg.get("tool_profile") or "core"
    bank = MemoryBank(workspace=workspace)   # 单作用域 ~/.pcode/memory/，workspace只给验尸用
    discovered = discover_skills(workspace)
    retrieval = is_retrieval_mode(workspace)          # regime 判定：>MENU_MAX 切检索
    if retrieval:
        skill_menu = render_retrieval_hint(len(discovered))
        skill_search = lambda task, cu: injection_block(workspace, task, cu)
    else:
        skill_menu = render_skill_menu(discovered)
        skill_search = None
    # ── 权限与安全审查（第4层：人工确认门）──────────────────────
    # 规则过滤/工具自检/外部内容标记在工具层永远生效；确认门只有 TTY 会话才有，
    # 这里把 y/a/n 回调装进闸里。"a"只在本会话免重复问，落盘总允许走 permissions.json
    def _confirm(cmd_sig: str, reason: str) -> str:
        console.print(Panel(f"[bold]命令:[/bold] {cmd_sig}\n[bold]命中:[/bold] {reason}",
                            title="⚠ 危险命令需要人工确认", border_style="yellow"))
        ans = console.input(
            "[bold yellow]放行？[y]仅这一次 [a]本会话内总是放行 [n]拒绝: [/bold yellow]"
        ).strip().lower()
        return {"y": "once", "a": "always"}.get(ans, "deny")

    install_permission_gate(PermissionManager(prompt=_confirm))

    console.print(f"[dim]记忆库: {bank.root}（{len(bank.entries)}条） · "
                  f"工具档: {profile} · 技能: {len(discovered)}个"
                  f"（{'检索' if retrieval else '菜单'}模式） · 门禁: 已武装[/dim]")
    return Agent(
        llm=llm,
        tools=create_tool_collection_profiled(profile, workspace=workspace),
        max_steps=50,
        memory_bank=bank,
        skill_menu=skill_menu,
        skill_search=skill_search,
        on_phase=lambda phase, step: console.print(
            f"[dim]📍 相位 → [bold]{phase}[/bold] (第{step}步)[/dim]"
        ),
    )


def show_sessions(workspace: str):
    metas = list_sessions(workspace=workspace)
    if not metas:
        console.print("[dim]本工作区还没有历史会话。[/dim]")
        return
    lines = ["| 会话ID | 标题 | 消息数 | 最近更新 |", "|---|---|---|---|"]
    for m in metas:
        updated = time.strftime("%m-%d %H:%M", time.localtime(m["updated_at"]))
        lines.append(f"| {m['session_id']} | {m['title'] or '（无标题）'} | {m['message_count']} | {updated} |")
    console.print(Panel(Markdown("\n".join(lines)), title="本工作区历史会话（按最近更新排序）"))


def main():
    parser = argparse.ArgumentParser(description="Pcode — 轻量级终端编码Agent")
    parser.add_argument("--resume", default=None, metavar="latest|ID",
                        help="启动时恢复会话: latest=本工作区最近的会话, 或指定会话ID")
    parser.add_argument("--sessions", action="store_true",
                        help="只列出本工作区的历史会话，不启动Agent（不需要API Key）")
    args = parser.parse_args()

    workspace = str(Path.cwd().resolve())

    if args.sessions:                     # 列出会话即可走，无需模型配置
        show_sessions(workspace)
        return

    agent = build_agent(workspace)

    # 会话决策（复刻 MiniCode 的 load_or_create_session 三分支）
    if args.resume == "latest":
        session = get_latest_session(workspace)
        if session:
            set_session(session.session_id)   # 工具落盘跟着会话走
            console.print(f"[green]已恢复本工作区最新会话 {session.session_id}[/green] "
                          f"[dim]（{session.title or '无标题'} · {len(session.messages)}条消息）[/dim]")
        else:
            console.print("[yellow]本工作区没有旧会话，开新会话。[/yellow]")
            session = Session(workspace=workspace)
            set_session(session.session_id)
    elif args.resume:
        session = load_session(args.resume)
        if session is None:
            console.print(f"[red]会话 {args.resume} 不存在。[/red]")
            raise SystemExit(1)
        set_session(session.session_id)
        console.print(f"[green]已恢复会话 {session.session_id}[/green] "
                      f"[dim]（工作区: {session.workspace}）[/dim]")
    else:
        # MiniCode 规则：默认开新会话；有旧会话只提示，不自动恢复
        session = Session(workspace=workspace)
        set_session(session.session_id)          # 工具层落盘按会话分文件夹
        latest = get_latest_session(workspace)
        if latest:
            console.print(
                f"[dim]本工作区有旧会话 {latest.session_id}（{latest.title or '无标题'}），"
                f"用 --resume latest 或 /resume latest 可继续[/dim]"
            )

    console.print(f"[bold cyan]{BANNER}[/bold cyan]")
    console.print(Panel(
        "输入任务，或 [bold cyan]/help[/bold cyan] 查看命令，[bold red]/exit[/bold red] 退出\n"
        f"[dim]工具箱:[/dim] {', '.join(agent.tools.tools)}"
    ))

    while True:
        try:
            text = console.input("[bold green]你 > [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not text:
            continue

        # ── 命令区 ─────────────────────────────────────
        if text == "/exit":
            break
        if text == "/help":
            console.print(Panel(Markdown(HELP_TEXT), title="帮助"))
            continue
        if text == "/tools":
            console.print(Panel(agent.tools.describe(), title="工具箱"))
            continue
        if text == "/new":
            session.save()
            session = Session(workspace=workspace)
            set_session(session.session_id)
            console.print("[green]已开新会话（旧会话已保存，/sessions 可见）[/green]")
            continue
        if text == "/sessions":
            show_sessions(workspace)
            continue
        if text == "/replay":
            show_replay(session)
            continue
        if text.startswith("/resume"):
            parts = text.split(maxsplit=1)
            target = parts[1] if len(parts) > 1 else "latest"
            loaded = (get_latest_session(workspace) if target == "latest"
                      else load_session(target))
            if loaded is None:
                console.print(f"[red]会话 {target} 不存在。/sessions 查看本工作区的会话。[/red]")
                continue
            session.save()                       # 当前对话先存盘再切换
            session = loaded
            set_session(session.session_id)
            console.print(
                f"[green]已恢复会话 {session.session_id}[/green] "
                f"[dim]（{session.title or '无标题'} · {len(session.messages)}条消息 · "
                f"工作区: {session.workspace}）[/dim]"
            )
            continue
        if text.startswith("/"):
            console.print(f"[red]未知命令: {text}[/red]（/help 查看全部）")
            continue

        # ── 任务区 ─────────────────────────────────────
        with console.status("[cyan]Pcode 工作中...[/cyan]"):
            try:
                answer = agent.run(text, session=session,
                                   on_tool=lambda desc, preview:
                                   console.print(f"[dim]🔧 {desc} → {preview}[/dim]"))
            except KeyboardInterrupt:
                answer = "（本轮任务被你中断）"
            except Exception as e:
                agent.report_failure()           # 💥 追责：本轮注入过的记忆−1
                answer = f"**出错了**: {e}"

        try:
            session.save()                       # 每回合结束自动存盘
        except Exception as e:
            console.print(f"[red]⚠ 会话保存失败（对话继续，但历史可能没落盘）: {e}[/red]")
        console.print(Panel(Markdown(answer), title="Pcode", border_style="cyan"))

    try:
        session.save()
    except Exception as e:
        console.print(f"[red]⚠ 退出时保存失败: {e}[/red]")
    console.print("[dim]再见！会话已保存，下次 /resume latest 接着聊。[/dim]")


if __name__ == "__main__":
    main()
