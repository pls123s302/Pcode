"""记忆库：跨会话的知识沉淀（复刻 MiniCode memory.py 的核心机制）

设计对齐（详见对话定稿）：
- 双格式：memory.json 是唯一事实源（完整元数据），MEMORY.md 是每次保存时
  重新生成的人类投影——改 json 有效，改 md 会被覆盖
- 原子写入：临时文件 + os.replace，进程被杀不会写坏
- 反馈驱动进化：注入过的记忆随任务成败 +2/-1，检索排序读这个计数——
  好用的记忆浮上来，误导的记忆沉底
- 策展人：每 MAINTAIN_INTERVAL 个任务跑一轮（去重/验尸/LLM熬洞见），
  这是全系统唯一花 LLM 的地方
- 单作用域 ~/.pcode/memory/：个人工具不需要项目库（YAGNI）
"""
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

MEMORY_ROOT = Path.home() / ".pcode" / "memory"

MAX_ENTRIES = 200            # 单库条目上限（MiniCode标注的 Claude Code 同款）
MAX_SIZE_BYTES = 25 * 1024   # 单库体积上限
MAINTAIN_INTERVAL = 10       # 每10个任务策展一次
TOP_K = 3                    # 每次注入最多几条（剂量控制）
CAP_CHARS = 150              # 注入时单条内容截断
GATE_SCORE = 0.25            # 检索相关度门槛，低于此不注入
DEDUP_JACCARD = 0.85         # 近重复判定线（留长的删短的）
CLUSTER_JACCARD = 0.50       # 洞见聚簇线
STALE_MARK = "[已过时: 提及的文件已不存在] "

_CATEGORY_TITLES = {"task_context": "任务事实", "reflection": "经验教训",
                    "insight": "洞见", "general": "其他"}

# 中英混合分词：英文按词，中文按二元字组（"配置文件"→配置/置文/文件）
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 事实网同款：从文本里抠"像文件路径"的片段（必须有点+扩展名，"."不算）
_PATH_RE = re.compile(r"[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9_]{1,8}")


def _tokenize(text: str) -> set:
    words = set(m.group(0).lower() for m in _TOKEN_RE.finditer(text))
    for run in _HAN_RE.findall(text):
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _atomic_write(target: Path, content: str) -> None:
    """原子落盘：先写临时文件再 os.replace（MiniCode 同款）"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix("." + uuid.uuid4().hex[:6] + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


class MemoryBank:
    """一个进程一个实例：构造时读盘，每次变更立即原子写回"""

    def __init__(self, root: Path | None = None, workspace: str | None = None):
        # workspace 仅用于策展验尸（检查条目提到的文件是否还存在），可为空
        self.root = Path(root) if root else MEMORY_ROOT
        self.workspace = workspace
        self.task_count = 0
        self.entries = []            # list[dict]，json 事实源的内存态
        self._load()

    # ── 存取 ────────────────────────────────────────────────
    def _load(self):
        f = self.root / "memory.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            self.entries = [e for e in data.get("entries", []) if e.get("id")]
            self.task_count = int(data.get("task_count", 0))
        except (json.JSONDecodeError, OSError):
            self.entries = []        # 坏了当空库起，绝不让记忆系统炸主流程

    def _save(self):
        _atomic_write(self.root / "memory.json", json.dumps(
            {"task_count": self.task_count, "entries": self.entries},
            ensure_ascii=False, indent=1))
        _atomic_write(self.root / "MEMORY.md", self._render_markdown())

    def _render_markdown(self) -> str:
        """MEMORY.md 投影：按类目分组，人能看懂（改它没用，会被覆盖）"""
        lines = ["# Pcode 记忆库", "",
                 f"*最后更新: {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"共 {len(self.entries)} 条*", ""]
        groups = {}
        for e in self.entries:
            groups.setdefault(e.get("category", "general"), []).append(e)
        for cat, items in groups.items():
            lines.append(f"## {_CATEGORY_TITLES.get(cat, cat)}")
            lines.append("")
            for e in items:
                tags = " `" + " ".join(e.get("tags", [])) + "`" if e.get("tags") else ""
                lines.append(f"- {e['content'][:200]}{tags}")
            lines.append("")
        return "\n".join(lines)

    # ── 写入（只有反思/策展调用，模型运行时只读）──────────────
    def add_entry(self, category: str, content: str, tags: list) -> dict:
        entry = {
            "id": f"mem_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}",
            "category": category,
            "content": content[:1200],
            "tags": list(tags)[:8],
            "usage_count": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "last_accessed": time.time(),
        }
        self.entries.append(entry)
        self._prune()
        self._save()
        return entry

    def _prune(self):
        """超限挤人：条数或体积超了，按最老优先淘汰"""
        while self.entries and (
                len(self.entries) > MAX_ENTRIES
                or sum(len(e["content"]) for e in self.entries) > MAX_SIZE_BYTES):
            oldest = min(range(len(self.entries)),
                         key=lambda i: self.entries[i]["created_at"])
            self.entries.pop(oldest)

    # ── 检索 + 注入（任务开始时由 agent 调用，零LLM）─────────
    def search(self, task: str, top_k: int = TOP_K) -> list:
        tks = _tokenize(task)
        tlow = task.lower()
        now = time.time()
        scored = []
        for e in self.entries:
            content = e.get("content", "")
            if content.startswith(STALE_MARK):
                continue                       # 过时记忆留档不注入
            score = len(tks & _tokenize(content + " " + " ".join(e.get("tags", [])))) \
                / max(1, len(tks))                       # 词重叠是主分
            if tlow and tlow in content.lower():
                score += 0.3                             # 任务原文被子串命中
            if any(t in tlow for t in e.get("tags", [])):
                score += 0.2                             # 标签出现在任务里
            age_h = (now - e.get("updated_at", now)) / 3600
            if age_h < 24:
                score += 0.1                             # 新近加权
            elif age_h < 24 * 7:
                score += 0.05
            score += min(0.2, math.log1p(e.get("usage_count", 0)) * 0.1)  # 反馈兑现成排名
            if score >= GATE_SCORE:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], -x[1].get("usage_count", 0)))
        return [e for _, e in scored[:top_k]]

    def injection_block(self, task: str, context_usage: float) -> tuple:
        """返回 (记忆块文本, 注入id列表)。上下文高压时拒绝注入（控制器闸门）"""
        if context_usage >= 0.75:
            return "", []
        hits = self.search(task)
        if not hits:
            return "", []
        lines = ["<记忆上下文>"]
        ids = []
        for i, e in enumerate(hits, 1):
            lines.append(f"{i}. [{e['category']}] {e['content'][:CAP_CHARS]}")
            ids.append(e["id"])
            e["last_accessed"] = time.time()     # 被检索也算一种使用
        lines.append("</记忆上下文>")
        self._save()
        return "\n".join(lines), ids

    # ── 反馈（自进化的发动机）────────────────────────────────
    def feedback(self, success: bool, ids: list):
        """任务成败 → 当事记忆加减分：成功+2，失败-1（可能误导了模型）"""
        changed = False
        for e in self.entries:
            if e["id"] in ids:
                e["usage_count"] = max(0, e["usage_count"] + (2 if success else -1))
                e["last_accessed"] = time.time()
                changed = True
        if changed:
            self._save()

    # ── 策展人（每 MAINTAIN_INTERVAL 个任务一次）──────────────
    def maintain(self, llm=None) -> dict | None:
        """去重 + 验尸 + LLM熬洞见。未到周期返回 None，到了返回报告"""
        self.task_count += 1
        if self.task_count < MAINTAIN_INTERVAL:
            self._save()         # 计数也必须落盘：否则一段无产出的会话退出即丢计数
            return None
        self.task_count = 0
        report = {"dedup": self._dedup(), "stale": self._validate_stale(),
                  "insights": self._consolidate(llm) if llm else 0}
        self._prune()
        self._save()
        return report

    def _dedup(self) -> int:
        """近重复归档：Jaccard≥0.85 的两条留长的删短的（信息已在长的那条里）"""
        removed = set()
        n = len(self.entries)
        for i in range(n):
            if i in removed or self.entries[i]["content"].startswith(STALE_MARK):
                continue
            for j in range(i + 1, n):
                if j in removed or self.entries[j]["content"].startswith(STALE_MARK):
                    continue
                if _jaccard(self.entries[i]["content"],
                            self.entries[j]["content"]) >= DEDUP_JACCARD:
                    short = j if len(self.entries[i]["content"]) >= len(
                        self.entries[j]["content"]) else i
                    removed.add(short)
        if removed:
            self.entries = [e for k, e in enumerate(self.entries) if k not in removed]
        return len(removed)

    def _validate_stale(self) -> int:
        """验尸：条目提到的文件全都不存在了 → 打过时标记（检索时跳过）"""
        if not self.workspace:
            return 0
        stale = 0
        for e in self.entries:
            c = e["content"]
            if c.startswith(STALE_MARK):
                continue
            paths = _PATH_RE.findall(c)
            if not paths:
                continue
            all_missing = True
            for p in paths[:3]:
                cand = Path(p)
                if cand.is_file() or (Path(self.workspace) / p.lstrip("/\\")).exists():
                    all_missing = False
                    break
            if all_missing:
                e["content"] = STALE_MARK + c[:120]
                stale += 1
        return stale

    def _consolidate(self, llm) -> int:
        """LLM熬洞见：相似条目聚簇（≥2条），每簇合成一句规律，本轮最多2条"""
        pool = [e for e in self.entries if not e["content"].startswith(STALE_MARK)]
        used, made = set(), 0
        for i, seed in enumerate(pool):
            if i in used or made >= 2:
                break
            cluster = [seed] + [other for k, other in enumerate(pool)
                                if k != i and k not in used
                                and _jaccard(seed["content"], other["content"])
                                >= CLUSTER_JACCARD]
            if len(cluster) < 2:
                continue
            used.update(pool.index(c) for c in cluster)
            texts = "\n".join(f"- {c['content'][:150]}" for c in cluster[:4])
            prompt = ("以下是来自历史任务的多条记忆，请提炼一句能概括它们共同规律的"
                      "中文经验，供AI编码助手日后参考。只输出这句话本身。\n\n" + texts)
            try:
                text = str(llm.chat([{"role": "user", "content": prompt}]).content or "").strip()
            except Exception:
                continue
            if 30 < len(text) < 500:
                self.add_entry("insight", text, ["curator-insight"])
                made += 1
        return made
