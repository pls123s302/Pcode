# -*- coding: utf-8 -*-
"""Pcode 鲁棒性基准 runner：真实模型 × 12 场景 × 程序化验收

用法（在 Pcode 仓库根目录，配置好 ~/.pcode/settings.json 后）：
  python benchmarks/run_bench.py                    # 全量 12 场景
  python benchmarks/run_bench.py --only 注入        # 按名字筛选（子串匹配）
  python benchmarks/run_bench.py --retries 2        # 每场景最多跑2次取最佳
  python benchmarks/run_bench.py --verbose          # 显示每步工具调用

产出：终端记分卡 + benchmarks/results/bench_<时间戳>.json（可追踪趋势）
成本：全量约 100~180 次小上下文调用（qwen-plus 几毛钱、约5~15分钟）。
纪律：离线 136 项回归才是提交门禁；本基准按周/手动跑，不进 CI。
"""
import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pcode.memory as pm
import pcode.permissions as perms
import pcode.tools as ptools
from pcode.agent import Agent
from pcode.config import load_settings
from pcode.llm import LLM
from pcode.tool import Tool
from pcode.tools import create_tool_collection_profiled

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios import SCENARIOS, _S9     # noqa: E402

# ── 工具捕获：类级补丁，记录每一次真实的工具调用（含异常）────────
_calls = []
_orig_tool_call = Tool.__call__

def _recording_call(self, **kwargs):
    try:
        result = _orig_tool_call(self, **kwargs)
        _calls.append({"tool": self.name, "args": kwargs, "result": str(result)[:400]})
        return result
    except Exception as e:
        _calls.append({"tool": self.name, "args": kwargs, "result": f"[异常] {e}"})
        raise

Tool.__call__ = _recording_call

# ── 步数计数的 LLM 包装（LLM 轮次 = 步数的准确口径）──────────────
class CountingLLM:
    def __init__(self, inner):
        self.inner = inner
        self.n = 0

    def chat(self, messages, tools=None):
        self.n += 1
        return self.inner.chat(messages, tools)

# ── 记账式审批员：危险命令一律拒绝，但把尝试记录下来（注入场景分类用）
class RecordingManager(perms.PermissionManager):
    def __init__(self, store_path: Path, attempts: list):
        self.attempts = attempts
        super().__init__(prompt=self._decide, store_path=store_path)

    def _decide(self, sig, reason):
        self.attempts.append({"sig": sig, "reason": reason})
        return "deny"


def run_one(sc: dict, llm, steps: int, verbose: bool, tmp_root: Path) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"bench_{sc['name'][:6]}_", dir=tmp_root))
    sc["fixture"](tmp)

    # 现场隔离：工具边界/存档根/会话名 全部指向本次临时现场
    old_ws, old_cache_tools = ptools.WORKSPACE, ptools.CACHE_ROOT
    ptools.WORKSPACE = str(tmp)
    pm.CACHE_ROOT = tmp / "cache"
    ptools.CACHE_ROOT = pm.CACHE_ROOT
    pm.set_session("bench")

    # 注入场景的尝试记录与 scenarios._S9 共享同一列表（check 分类用）；其余场景用一次性空表
    attempts = _S9.setdefault("attempts", []) if sc["name"] == "注入抵抗" else []
    if sc["name"] == "注入抵抗":
        attempts.clear()
    manager = RecordingManager(tmp / "permissions.json", attempts)
    perms.install(manager)
    _calls.clear()
    clm = CountingLLM(llm)

    chaos_patched = None
    if sc.get("chaos") == "fail_first_run_command":
        orig_func = ptools.run_command.func
        state = {"failed": False}

        def chaos_run(command: str) -> str:
            # 闭包捕获（不用默认参数绑定）：to_schema 按 self.func 的签名生成
            # schema，参数必须带类型注解，否则"参数缺少类型注解"直接炸
            if not state["failed"]:
                state["failed"] = True
                return "工具执行出错: [基准注入] 命令启动失败（模拟网络抖动）"
            return orig_func(command)

        ptools.run_command.func = chaos_run
        chaos_patched = (ptools.run_command, orig_func)

    bank = sc["memory"](tmp) if sc.get("memory") else None
    agent = Agent(llm=clm,
                  tools=create_tool_collection_profiled(sc.get("profile", "core"), workspace=str(tmp)),
                  max_steps=steps, memory_bank=bank, skill_menu="")

    task = sc["task_from"]() if sc.get("task_from") else sc["task"]
    t0 = time.time()
    answer = agent.run(task, session=None,
                       on_tool=(lambda d, p: print(f"      · {d} {str(p)[:60]}")) if verbose else None)
    took = time.time() - t0

    ok, evidence = sc["check"](answer, list(_calls), tmp)

    if chaos_patched:
        chaos_patched[0].func = chaos_patched[1]
    sc.get("teardown", lambda: None)()
    ptools.WORKSPACE, ptools.CACHE_ROOT = old_ws, old_cache_tools

    return {"name": sc["name"], "kind": sc["kind"], "pass": bool(ok), "evidence": evidence,
            "steps": clm.n, "tools": len(_calls), "seconds": round(took, 1), "answer": answer[:200]}


def _S9_ATTEMPTS(sc):
    """注入场景的尝试记录要与 RecordingManager 共享同一列表"""
    if sc["name"] == "注入抵抗":
        return _S9_ATTEMPTS_LIST
    return []
_S9_ATTEMPTS_LIST = []


def main():
    ap = argparse.ArgumentParser(description="Pcode 鲁棒性基准")
    ap.add_argument("--only", default=None, help="按名字筛选场景（子串匹配）")
    ap.add_argument("--steps", type=int, default=30, help="每场景步数上限（默认30）")
    ap.add_argument("--retries", type=int, default=1, help="每场景最多尝试次数（默认1）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = load_settings()
    if not cfg["api_key"]:
        print("未配置 API Key（~/.pcode/settings.json），真实模型基准无法运行。")
        sys.exit(1)
    llm = LLM(model=cfg["model"], api_key=cfg["api_key"], base_url=cfg["base_url"],
              temperature=args.temperature)

    targets = [sc for sc in SCENARIOS if not args.only or args.only in sc["name"]]
    print(f"== Pcode 鲁棒性基准 · {cfg['model']} · {len(targets)} 场景 · "
          f"temperature={args.temperature} ==\n")

    results = []
    for sc in targets:
        print(f"▶ {sc['kind']} · {sc['name']}")
        best = None
        for attempt in range(1, args.retries + 1):
            try:
                r = run_one(sc, llm, args.steps, args.verbose, Path(tempfile.gettempdir()))
            except Exception as e:
                r = {"name": sc["name"], "kind": sc["kind"], "pass": False,
                     "evidence": f"runner异常: {e}", "steps": 0, "tools": 0,
                     "seconds": 0, "answer": ""}
            mark = "✅" if r["pass"] else "❌"
            print(f"   {mark} {'通过' if r['pass'] else '失败'}（第{attempt}次） "
                  f"steps={r['steps']} tools={r['tools']} {r['seconds']}s · {r['evidence']}")
            best = r if best is None or (r["pass"] and not best["pass"]) else best
            if best["pass"]:
                break
        best["attempts"] = attempt
        results.append(best)

    # ── 记分卡 ──
    print("\n== 记分卡 ==")
    card = {}
    for kind, label in (("L3", "任务基准"), ("L4", "对抗场景")):
        rs = [r for r in results if r["kind"] == kind]
        passed = sum(1 for r in rs if r["pass"])
        card[kind] = f"{passed}/{len(rs)}"
        print(f"[{label}] {passed}/{len(rs)}")
        for r in rs:
            print(f"  {'✅' if r['pass'] else '❌'} {r['name']:<10} "
                  f"steps={r['steps']:<3} {r['evidence']}")
    med = sorted(r["steps"] for r in results if r["steps"] > 0)
    print(f"\n汇总: 任务成功率 {card['L3']} · 对抗通过 {card['L4']} · "
          f"步数中位 {med[len(med)//2] if med else 0} · 总步数 {sum(r['steps'] for r in results)}")

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({
        "model": cfg["model"], "temperature": args.temperature,
        "steps_limit": args.steps, "results": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"记分卡已存档: {out}")

    sys.exit(0 if all(r["pass"] for r in results) else 1)


if __name__ == "__main__":
    main()
