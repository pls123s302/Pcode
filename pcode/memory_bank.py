"""记忆库：跨会话的知识沉淀（复刻 MiniCode memory.py 核心机制 + 双作用域升级）

作用域（都在 ~/.pcode 下按层级划分）：
- 用户级  ~/.pcode/memory/                        —— 跨项目通用经验（如用户偏好、
                                                     "Windows 先设 UTF-8 再调试"）
- 项目级  ~/.pcode/projects/<工作区键>/memory/     —— 项目专属记忆与状态（按工作区
                                                     划分；留在用户目录下与项目仓库
                                                     解耦——不会误提交，集中备份）

设计对齐（详见对话定稿）：
- 双格式：memory.json 唯一事实源（schema_version/task_count/entries/states），
  MEMORY.md 人类投影（改了会被覆盖）；原子写入：临时文件 + os.replace
- 反馈驱动进化：注入过的记忆随任务成败 +2/-1，检索排序读这个计数——
  好用的记忆浮上来，误导的记忆沉底
- 检索质量机：BM25 + 查询扩展三词典（中英术语/会话别名/领域）+ 六路混合评分
  （抄 MiniCode memory.py），跨作用域同一语料打分（避免弱命中遮强命中），
  门槛为相对值
- 生命周期机：tier 四层升降级（转正/档案化摘要/复活）+ 超龄使用衰减 +
  写入冲突检测（抄 MiniCode promote/decay/detect_conflicts）
- 状态账本：世界现在是什么样（同 key 新值覆盖旧值），项目级默认、user: 前缀入用户级；
  手动状态（# state:）优先注入且不褪色，自动状态按确认窗衰减淘汰（有生命周期但极轻量）
- 策展人：每 MAINTAIN_INTERVAL 个任务一轮（去重/验尸/衰减/升降级/LLM熬洞见），
  全系统唯一花 LLM 的地方

兼容性：workspace=None 时项目库停用，所有写入落用户库——老调用方
（MemoryBank(root=临时目录)）行为与单作用域版完全一致；老 memory.json
（无 schema_version/states）直接读，零迁移。
"""
import json
import math
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path

from pcode import paths

MEMORY_ROOT = Path.home() / ".pcode" / "memory"        # 用户级（跨项目通用）
# 项目级根：~/.pcode/projects/<工作区键>/memory/（按工作区划分，键见 paths.workspace_key）

TOP_K = 3                    # 每次注入最多几条（剂量上限，随压力自适应收缩）
CAP_CHARS = 150              # 注入时单条内容截断
GATE_SCORE = 0.25            # 检索相对门槛（低于最佳分的该比例不注入）
STALE_MARK = "[已过时: 提及的文件已不存在] "
DEDUP_JACCARD = 0.85         # 近重复判定线（留长的删短的）
CLUSTER_JACCARD = 0.50       # 洞见聚簇线
MAINTAIN_INTERVAL = 10       # 每10个任务策展一次
MAX_ENTRIES = 200            # 单库条目上限（MiniCode标注的 Claude Code 同款）
MAX_SIZE_BYTES = 25 * 1024   # 单库体积上限
SCHEMA_VERSION = 1

# ── 状态层生命周期：自动捕获是便签会褪色，显式写入是钉子不褪色 ──
_STATE_CONFIRM_DAYS = 14     # 确认窗：自动状态超过这么久没被重新确认开始褪色
_STATE_DECAY_FACTOR = 0.5    # 每个确认窗有效置信度减半
_STATE_EVICT_CONFIDENCE = 0.2  # 退场线（0.75起步约28天未再确认即出局）
MAX_STATES = 64              # 单库状态键硬顶（淘汰顺序：自动先于手动/低置信/最老）

_CATEGORY_TITLES = {"task_context": "任务事实", "reflection": "经验教训",
                    "insight": "洞见", "directive": "指令", "general": "其他"}


# ── 基础分词（集合语义，Jaccard 去重/聚簇用）─────────────────────
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


# ── 检索质量机（BM25 + 查询扩展 + 六路混合评分，抄 MiniCode memory.py）──
# 裸词重叠的三个痛点在这里逐个解决：
#   常见虚词淹没真信号   → IDF 压制 + 停用词过滤（含中文虚词 bigram，Pcode 增补）
#   同义词/中英文撞不上   → 查询扩展三词典（代码术语双向 / 会话别名 / 领域词典）
#   评分只有一路信号      → BM25+子串+标签+域Jaccard+使用+新近度 六路软融合

_BM25_K1 = 1.5      # 词频缩放（MiniCode 同款）
_BM25_B = 0.75      # 文档长度归一

# 查询停用词（只过滤检索侧，存储内容一字不动）
_SEARCH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "did",
    "do", "does", "for", "from", "how", "in", "is", "it", "of", "on", "or",
    "should", "that", "the", "this", "to", "use", "using", "we", "what",
    "when", "where", "which", "who", "why", "would",
})

# 大语料自适应：库超 1000 条且问句是推荐/偏好类时，口语填充词的文档频率
# 会盖过真信号，切换更严的停用词表（MiniCode 同款机制）
_LARGE_CORPUS_THRESHOLD = 1000
_LARGE_CORPUS_QUERY_MARKERS = ("recommend", "suggest", "advice", "tips",
                               "what should", "prefer", "推荐", "建议", "偏好",
                               "选择", "怎么选")
_LARGE_CORPUS_SEARCH_STOPWORDS = frozenset({
    "also", "any", "around", "find", "get", "give", "i", "just", "like",
    "many", "me", "might", "mine", "more", "much", "my", "one", "please",
    "really", "some", "still", "tell", "thing", "things", "think", "you",
    "your", "yours",
})

# 中文虚词 bigram 过滤（Pcode 增补）：任务里的"里的/是多少/这个"等虚词
# bigram 会撞上所有中文卡，检索侧过滤——MiniCode 未做这个适配
_CJK_STOP_BIGRAMS = frozenset({
    "里的", "的是", "是是", "这个", "那个", "一个", "多少", "怎么", "什么",
    "我的", "你的", "还有", "以及", "然后", "一下", "哪些", "要不", "就是",
})

# 中英代码术语双向表（问"函数"也能撞上写了 function 的卡）
_CODE_TERM_EXPANSIONS = {
    "函数": ["function", "func", "method"], "function": ["函数", "func", "method"],
    "method": ["方法", "function", "func"], "类": ["class", "type"],
    "class": ["类", "type"], "变量": ["variable", "var"],
    "variable": ["变量", "var"], "参数": ["parameter", "param", "argument"],
    "parameter": ["参数", "param", "argument"], "argument": ["参数", "parameter"],
    "属性": ["attribute", "property"], "attribute": ["属性", "property"],
    "接口": ["interface"], "interface": ["接口"], "模块": ["module"],
    "module": ["模块"], "对象": ["object"], "object": ["对象"],
    "异常": ["exception", "error"], "exception": ["异常", "error"],
    "error": ["错误", "异常"], "错误": ["error", "bug"], "bug": ["错误", "缺陷"],
    "循环": ["loop"], "loop": ["循环"], "数组": ["array"], "array": ["数组"],
    "列表": ["list"], "list": ["列表"], "字典": ["dict", "map"],
    "dict": ["字典", "map"], "字符串": ["string"], "string": ["字符串"],
    "异步": ["async"], "async": ["异步"], "回调": ["callback"],
    "callback": ["回调"], "装饰器": ["decorator"], "decorator": ["装饰器"],
    "测试": ["test", "testing"], "test": ["测试", "testing"],
    "调试": ["debug"], "debug": ["调试"], "配置": ["config"],
    "config": ["配置"], "数据库": ["database", "db"], "database": ["数据库", "db"],
    "缓存": ["cache"], "cache": ["缓存"], "队列": ["queue"], "queue": ["队列"],
    "路径": ["path"], "path": ["路径"], "请求": ["request"],
    "request": ["请求"], "响应": ["response"], "response": ["响应"],
    "排序": ["sort"], "sort": ["排序"], "文件": ["file"], "file": ["文件"],
}

# 会话别名词表（问句常改写当时用词：recommend↔suggest 这类，查询侧扩展）
_CONVERSATIONAL_TERM_EXPANSIONS = {
    "recommend": ["suggest", "advice"], "suggest": ["recommend", "advice"],
    "recommendation": ["recommend", "suggest"], "prefer": ["favorite", "like", "choice"],
    "preference": ["prefer", "favorite"], "favorite": ["prefer", "like"],
    "choose": ["choice", "select", "prefer"], "choice": ["choose", "select"],
    "remember": ["recall", "previously"], "previously": ["before", "earlier"],
}

# 领域词典：按任务自身命中的领域选择性扩展（不引入 MiniCode 的独立分类器，
# 直接用 _auto_domains 的关键词表兼任——一套表两个用途）
_DOMAIN_TERM_EXPANSIONS = {
    "frontend": {"component": ["组件", "控件"], "组件": ["component", "widget"],
                 "样式": ["style", "css"], "css": ["样式", "style"],
                 "渲染": ["render", "display"], "路由": ["router", "route"],
                 "布局": ["layout", "flex"], "页面": ["page", "view"]},
    "backend": {"api": ["端点", "endpoint", "route"], "端点": ["api", "endpoint"],
                "鉴权": ["auth", "jwt", "认证"], "认证": ["auth", "jwt"],
                "中间件": ["middleware"], "middleware": ["中间件"],
                "限流": ["rate limit", "throttle"], "服务器": ["server", "host"]},
    "database": {"迁移": ["migration", "schema"], "migration": ["迁移"],
                 "索引": ["index"], "index": ["索引"], "事务": ["transaction", "commit"],
                 "transaction": ["事务"], "查询": ["query", "select"],
                 "query": ["查询", "select"], "备份": ["backup", "dump"]},
    "devops": {"部署": ["deploy", "release"], "deploy": ["部署", "release"],
               "容器": ["docker", "container"], "docker": ["容器"],
               "流水线": ["pipeline", "ci"], "监控": ["monitor", "metrics"],
               "密钥": ["secret", "credentials"], "发布": ["release", "deploy"]},
    "testing": {"断言": ["assert", "expect"], "assert": ["断言", "expect"],
                "mock": ["模拟", "stub"], "模拟": ["mock", "stub"],
                "覆盖率": ["coverage"], "coverage": ["覆盖率"],
                "回归": ["regression"], "regression": ["回归"],
                "夹具": ["fixture"], "fixture": ["夹具", "setup"]},
}

# 领域自动分类：关键词计数取前2（同时服务查询扩展的领域门与域加权信号）
_DOMAIN_KEYWORDS = {
    "frontend": ["前端", "组件", "样式", "css", "react", "vue", "渲染", "按钮",
                 "布局", "页面", "html", "路由"],
    "backend": ["后端", "接口", "api", "端点", "服务", "中间件", "鉴权", "认证",
                "jwt", "限流", "server", "请求"],
    "database": ["数据库", "sql", "查询", "索引", "事务", "迁移", "schema",
                 "mysql", "postgres", "redis"],
    "devops": ["部署", "docker", "k8s", "kubernetes", "ci", "流水线", "监控",
               "nginx", "terraform", "发布"],
    "testing": ["测试", "断言", "mock", "覆盖率", "单元", "回归", "pytest",
                "unittest", "用例"],
}


def _auto_domains(text: str) -> list:
    """按关键词计数自动判领域，返回最多2个。无命中返回空表（域信号缺席≠惩罚）"""
    low = text.lower()
    scored = sorted(((d, sum(1 for kw in kws if kw in low))
                     for d, kws in _DOMAIN_KEYWORDS.items()),
                    key=lambda x: -x[1])
    return [d for d, s in scored if s > 0][:2]


@lru_cache(maxsize=2048)
def _tokenize_list(text: str) -> tuple:
    """频次保留版分词（BM25 用）：英文词 + 中文字 + 中文二元组"""
    words = [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]
    for run in _HAN_RE.findall(text):
        if len(run) == 1:
            words.append(run)
        else:
            words.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tuple(words)


def _search_tokens(text: str, stopwords: frozenset) -> list:
    """检索分词：去掉停用词与中文虚词 bigram（原始词，供子串/标签检查）"""
    return [t for t in _tokenize_list(text)
            if t not in stopwords and t not in _CJK_STOP_BIGRAMS]


def _uses_large_corpus_stopwords(query: str) -> bool:
    return any(m in query.lower() for m in _LARGE_CORPUS_QUERY_MARKERS)


def _expand_query_terms(terms: list, active_domains: list | None = None) -> list:
    """查询扩展：词 → 同义词家族（代码术语 + 会话别名 + 命中领域专属）"""
    expanded = list(terms)
    for term in terms:
        expanded.extend(_CODE_TERM_EXPANSIONS.get(term, []))
        expanded.extend(_CONVERSATIONAL_TERM_EXPANSIONS.get(term, []))
    if active_domains:
        for d in active_domains:
            table = _DOMAIN_TERM_EXPANSIONS.get(d, {})
            for term in terms:
                expanded.extend(table.get(term, []))
    return expanded


def _compute_idf(docs: list) -> dict:
    """平滑 IDF：log((N+1)/(df+1))+1 —— 常见词被压低，稀有词信号放大"""
    n = len(docs)
    if n == 0:
        return {}
    df = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    return {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}


def _compute_avgdl(docs: list) -> float:
    return sum(len(d) for d in docs) / len(docs) if docs else 0.0


def _bm25_score(q_tokens: list, doc_tokens: list, idf: dict, avgdl: float) -> float:
    """Okapi BM25：IDF(qi) · tf·(k1+1) / (tf + k1·(1−b+b·|d|/avgdl))"""
    if not q_tokens or not doc_tokens or avgdl == 0:
        return 0.0
    tf = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    for term in set(q_tokens):
        f = tf.get(term, 0)
        if f == 0 or term not in idf:
            continue
        score += idf[term] * (f * (_BM25_K1 + 1)) / (
            f + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(doc_tokens) / avgdl))
    return score


# ── 生命周期机（tier 升降级 + 使用衰减，抄 MiniCode promote/decay）──────
# 治两个病：①老卡靠历史积分永久霸榜（usage 只涨不减）②30天无人问津的记忆
# 永远占着全文。方案是人类记忆的分层模型（Atkinson-Shiffrin / MemGPT 同款）：
# WORKING→SHORT_TERM→LONG_TERM→ARCHIVAL，越老越没人用越压缩，被用就复活。

_DECAY_MAX_AGE_DAYS = 30.0    # 超过这个年龄的记忆，usage_count 减半
_DECAY_FACTOR = 0.5


def _summarize_content(content: str, max_len: int = 150) -> str:
    """档案化摘要：优先保住第一句，超长截断（MiniCode 同款）"""
    if len(content) <= max_len:
        return content
    for sep in (". ", ".\n", "; ", ";\n", "\n", "。"):    # 句号分句为 Pcode 增补
        idx = content.find(sep)
        if 20 < idx < max_len:
            return content[:idx + 1]
    return content[:max_len] + "..."


def _state_confidence(rec: dict, now: float) -> float:
    """状态的有效置信度（纯函数，随时间流逝自动褪色，不落盘不叠加）：
    手动状态（# state:，evidence_id=manual）不褪色——用户点名的就是确凿的；
    自动状态按确认窗衰减——同 key 重新写入即刷新 updated_at，等于续命"""
    if rec.get("evidence_id") == "manual":
        return 1.0
    windows = int(max(0.0, (now - rec.get("updated_at", now)) / 86400.0)
                  // _STATE_CONFIRM_DAYS)
    return rec.get("confidence", 0.75) * (_STATE_DECAY_FACTOR ** windows)


class _Store:
    """单作用域存储（entries + states + 策展计数），机械方法住这里；
    跨作用域的路由与合并语义（检索聚合/回填/默认域）住 MemoryBank 门面"""

    def __init__(self, root: Path, scope: str, workspace: str | None = None):
        self.root = Path(root)
        self.scope = scope                        # "user" / "project"
        self.label = "项目" if scope == "project" else "通用"
        self.workspace = workspace                # 仅验尸用（项目库才有意义）
        self.entries = []
        self.states = {}
        self.task_count = 0
        self._load()

    # ── 存取 ──
    def _load(self):
        f = self.root / "memory.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            self.entries = [e for e in data.get("entries", []) if e.get("id")]
            self.task_count = int(data.get("task_count", 0))
            states = data.get("states", {})
            self.states = states if isinstance(states, dict) else {}
        except (json.JSONDecodeError, OSError):
            self.entries = []        # 坏了当空库起，绝不让记忆系统炸主流程
            self.states = {}

    def _save(self):
        _atomic_write(self.root / "memory.json", json.dumps(
            {"schema_version": SCHEMA_VERSION, "task_count": self.task_count,
             "entries": self.entries, "states": self.states},
            ensure_ascii=False, indent=1))
        _atomic_write(self.root / "MEMORY.md", self._render_markdown())

    def _render_markdown(self) -> str:
        """MEMORY.md 投影：按类目分组，人能看懂（改它没用，会被覆盖）"""
        scope_name = "项目记忆库" if self.scope == "project" else "用户记忆库（跨项目）"
        lines = [f"# {scope_name}", "",
                 f"*最后更新: {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"共 {len(self.entries)} 条 · 状态 {len(self.states)} 项*", ""]
        if self.states:
            lines.append("## 状态")
            lines.append("")
            for key, r in sorted(self.states.items(),
                                 key=lambda kv: -kv[1].get("updated_at", 0)):
                lines.append(f"- {key} = {r.get('value', '')}")
            lines.append("")
        groups = {}
        for e in self.entries:
            groups.setdefault(e.get("category", "general"), []).append(e)
        for cat, items in groups.items():
            lines.append(f"## {_CATEGORY_TITLES.get(cat, cat)}")
            lines.append("")
            for e in items:
                tags_str = f" `{' '.join(e.get('tags', []))}`" if e.get("tags") else ""
                lines.append(f"- {e['content'][:200]}{tags_str}")
            lines.append("")
        return "\n".join(lines)

    def _prune(self):
        """超限挤人：条数或体积超了，按最老优先淘汰"""
        while self.entries and (
                len(self.entries) > MAX_ENTRIES
                or sum(len(e["content"]) for e in self.entries) > MAX_SIZE_BYTES):
            oldest = min(range(len(self.entries)),
                         key=lambda i: self.entries[i]["created_at"])
            self.entries.pop(oldest)

    # ── 生命周期机械操作（facade 在策展周期统一调度）──
    def _dedup(self) -> int:
        """近重复归档（usage 继承）：精确重复先合并，再 Jaccard 近似合并保长版"""
        seen: dict = {}
        unique, exact_removed = [], 0
        for e in self.entries:
            key = e["content"].strip().lower()
            if key in seen:
                master = unique[seen[key]]
                master["usage_count"] = master.get("usage_count", 0) + e.get("usage_count", 0)
                master["updated_at"] = max(master.get("updated_at", 0), e.get("updated_at", 0))
                master["tags"] = sorted(set(master.get("tags", []) + e.get("tags", [])))
                exact_removed += 1
            else:
                seen[key] = len(unique)
                unique.append(e)
        removed = set()
        approx_removed = 0
        for i in range(len(unique)):
            if i in removed or unique[i]["content"].startswith(STALE_MARK):
                continue
            for j in range(i + 1, len(unique)):
                if j in removed or unique[j]["content"].startswith(STALE_MARK):
                    continue
                if _jaccard(unique[i]["content"],
                            unique[j]["content"]) >= DEDUP_JACCARD:
                    short = j if len(unique[i]["content"]) >= len(
                        unique[j]["content"]) else i
                    keeper = unique[i if short == j else j]
                    donor = unique[short]
                    keeper["usage_count"] = keeper.get("usage_count", 0) + donor.get("usage_count", 0)
                    keeper["tags"] = sorted(set(keeper.get("tags", []) + donor.get("tags", [])))
                    keeper["updated_at"] = max(keeper.get("updated_at", 0), donor.get("updated_at", 0))
                    removed.add(short)
                    approx_removed += 1
        if removed:
            self.entries = [e for k, e in enumerate(unique) if k not in removed]
        elif exact_removed:
            self.entries = unique
        return exact_removed + approx_removed

    def _validate_stale(self) -> int:
        """验尸：条目提到的文件全都不存在了 → 打过时标记（检索时跳过）"""
        if not self.workspace:
            return 0
        stale = 0
        now = time.time()
        for e in self.entries:
            c = e.get("content", "")
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
                e["updated_at"] = now
                stale += 1
        return stale

    def decay_memories(self, max_age_days: float = _DECAY_MAX_AGE_DAYS,
                       decay_factor: float = _DECAY_FACTOR) -> int:
        """时间衰减：超龄记忆的 usage_count 减半——旧经验值不再永久霸榜"""
        now = time.time()
        decayed = 0
        for e in self.entries:
            age_days = (now - e.get("updated_at", now)) / 86400.0
            if age_days > max_age_days and e.get("usage_count", 0) > 0:
                e["usage_count"] = max(0, int(e["usage_count"] * decay_factor))
                decayed += 1
        return decayed

    def decay_states(self) -> int:
        """状态褪色淘汰：自动状态过确认窗未再确认 → 有效置信度按窗数衰减，
        跌破退场线即删除。手动状态免疫。置信度是纯函数所以不写盘，
        真正的删除只在策展周期发生一次"""
        now = time.time()
        dead = [k for k, r in self.states.items()
                if _state_confidence(r, now) < _STATE_EVICT_CONFIDENCE]
        for k in dead:
            del self.states[k]
        return len(dead)

    def _prune_states(self) -> None:
        """状态键硬顶：超过 MAX_STATES 按淘汰序删人——自动先于手动、
        低置信先于高置信、老者先于新者"""
        overflow = len(self.states) - MAX_STATES
        if overflow <= 0:
            return
        ranked = sorted(self.states.items(),
                        key=lambda kv: (0 if kv[1].get("evidence_id") != "manual" else 1,
                                        kv[1].get("confidence", 0.75),
                                        kv[1].get("updated_at", 0)))
        for k, _ in ranked[:overflow]:
            del self.states[k]

    def promote_memories(self) -> dict:
        """tier 升降级（抄 MiniCode promote_memories，Atkinson-Shiffrin 分层）：
        SHORT_TERM 且 usage≥5 且超7天 → LONG_TERM；LONG_TERM 且 30天未访问 →
        ARCHIVAL（内容摘要到150字）；长/档 且 7天内被用且非过时 → 复活"""
        now = time.time()
        stats = {"promoted": 0, "archived": 0, "reactivated": 0}
        for e in self.entries:
            tier = e.get("tier", "short_term")
            age_days = (now - e.get("updated_at", now)) / 86400.0
            accessed_days = (now - e.get("last_accessed", now)) / 86400.0
            usage = e.get("usage_count", 0)
            if tier == "short_term" and usage >= 5 and age_days > 7:
                e["tier"] = "long_term"
                stats["promoted"] += 1
            if tier == "long_term" and accessed_days > 30:
                e["tier"] = "archival"
                e["content"] = _summarize_content(e["content"])
                e["domains"] = _auto_domains(e["content"])   # 内容变了，域缓存重算
                stats["archived"] += 1
            elif tier in ("long_term", "archival") and accessed_days < 7 \
                    and usage > 0 and not e.get("content", "").startswith(STALE_MARK):
                e["tier"] = "short_term"
                stats["reactivated"] += 1
        return stats

    def _consolidate(self, llm) -> int:
        """LLM熬洞见：相似条目聚簇（≥2条），每簇合成一句规律，本轮最多2条"""
        pool = [e for e in self.entries if not e.get("content", "").startswith(STALE_MARK)]
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
                rec = {"id": f"mem_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}",
                       "category": "insight", "content": text[:1200],
                       "tags": ["curator-insight"], "usage_count": 0,
                       "tier": "short_term",
                       "created_at": time.time(), "updated_at": time.time(),
                       "last_accessed": time.time()}
                self.entries.append(rec)
                self._prune()
                made += 1
        return made


class MemoryBank:
    """双作用域记忆库门面：用户级（跨项目通用）+ 项目级（项目专属，可进git）。

    检索时跨作用域同一语料打分（BM25 全局 IDF，避免弱命中遮强命中）；
    写入默认项目级（任务经验/状态天然项目语境），显式 scope="user" 或
    `# user:` / `# state:user:` 前缀才落用户级；workspace=None 时项目库停用，
    退化为单作用域（老调用方/测试零迁移）。"""

    def __init__(self, root: Path | None = None, workspace: str | None = None,
                 project_root: Path | None = None):
        self.user_root = Path(root) if root else MEMORY_ROOT
        # workspace 用于项目库划分与策展验尸；None = 项目库停用（单作用域兼容模式）
        self.workspace = workspace
        if workspace:
            proj = (Path(project_root) if project_root
                    else paths.project_dir(workspace) / "memory")
            self._project = _Store(proj, "project", workspace=str(workspace))
        else:
            self._project = None
        self._user = _Store(self.user_root, "user")
        self.task_count = max(self._user.task_count,
                              self._project.task_count if self._project else 0)

    # ── 内部路由 ──
    def _stores(self) -> list:
        return ([self._project] if self._project else []) + [self._user]

    def _store_for(self, scope: str):
        if scope == "user":
            return self._user
        return self._project if self._project is not None else self._user

    def _save_all(self):
        for store in self._stores():
            store.task_count = self.task_count
            store._save()

    # ── 事件卡：entries 兼容视图（跨库拼接，dict 引用共享可原位改）──
    @property
    def entries(self) -> list:
        out = []
        for store in self._stores():
            out.extend(store.entries)
        return out

    @property
    def states(self) -> dict:
        """合并状态视图：同 key 项目级覆盖用户级（项目语境更具体）"""
        merged = dict(self._user.states)
        if self._project:
            merged.update(self._project.states)
        return merged

    # ── 写入 ──
    def add_entry(self, category: str, content: str, tags: list,
                  scope: str = "project") -> dict:
        entry = {
            "id": f"mem_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}",
            "category": category,
            "content": content[:1200],
            "tags": list(tags)[:8],
            "usage_count": 0,
            "tier": "short_term",
            "created_at": time.time(),
            "updated_at": time.time(),
            "last_accessed": time.time(),
        }
        # 写入时冲突检测（抄 MiniCode detect_conflicts）：找出主题重叠的旧卡记在
        # 新卡上。只记录不拦截——Jaccard 分不出"矛盾"还是"同主题补充"，复核交策展人
        conflicts = self.detect_conflicts(entry["content"])
        if conflicts:
            entry["conflicts"] = [c["id"] for c, _ in conflicts[:5]]
        store = self._store_for(scope)
        store.entries.append(entry)
        store._prune()
        store._save()
        return entry

    def capture_directive(self, content: str, scope: str = "project") -> dict:
        """显式捕获（抄 MiniCode handle_user_memory_input 的 '#' 通道）：
        用户聊天里以 # 开头的一句话直接记账入库，不经反思四网。
        反思网是概率捕捞（捞不捞得到看措辞），显式指令是确定写入（说了就算）——
        "以后跑测试都用 Langgraph 环境"这种规定，必须绕过概率网。"""
        return self.add_entry("directive", content[:1200], ["chat"], scope=scope)

    # ── 状态账本 ──
    def set_state(self, key: str, value: str, evidence_id: str = "",
                  confidence: float = 0.75, scope: str = "project") -> dict:
        """状态写入：同 key 直接覆盖（状态是"现在"，新值天然取代旧值——
        毒化/过期问题在结构上消失）。evidence_id 指向支撑本状态的记忆卡；
        显式写入（manual）置信度记 1.0 且永不褪色，自动捕获按确认窗衰减"""
        store = self._store_for(scope)
        rec = {"key": key[:80], "value": value[:200],
               "evidence_id": evidence_id,
               "confidence": 1.0 if evidence_id == "manual" else confidence,
               "updated_at": time.time()}
        store.states[key[:80]] = rec
        store._prune_states()
        store._save()
        return rec

    def format_states(self, max_records: int = 12) -> str:
        """最新状态注入块：手动状态优先上场（用户点名的事实不缺席），
        自动状态按新近竞争剩余名额；褪色到退场线以下的不注入
        （与策展淘汰同一条线）。剂量固定 ≤max_records 行，不随压力收缩"""
        merged = self.states
        if not merged:
            return ""
        now = time.time()
        recs = [r for r in merged.values()
                if _state_confidence(r, now) >= _STATE_EVICT_CONFIDENCE]
        recs.sort(key=lambda r: (0 if r.get("evidence_id") == "manual" else 1,
                                 -r.get("updated_at", 0)))
        recs = recs[:max_records]
        if not recs:
            return ""
        lines = ["<最新状态>"]
        for r in recs:
            ts = time.strftime("%m-%d", time.localtime(r.get("updated_at", time.time())))
            lines.append(f"- [{ts}] {r['key']} = {r['value']} "
                         f"(证据: {r.get('evidence_id') or '无'})")
        lines.append("</最新状态>")
        return "\n".join(lines)

    # ── 检索（跨作用域同一语料）──
    def _search_ranked(self, task: str, top_k: int) -> list:
        """内部检索：返回 [(条目, 作用域标签)]，按六路混合分降序、过相对门槛"""
        pairs = []
        for store in self._stores():
            for e in store.entries:
                if e.get("content", "").startswith(STALE_MARK):
                    continue                                 # 过时记忆留档不注入
                pairs.append((e, store.label))
        if not pairs:
            return []
        now = time.time()
        q_low = task.lower()

        stopwords = _SEARCH_STOPWORDS
        if len(pairs) >= _LARGE_CORPUS_THRESHOLD and _uses_large_corpus_stopwords(task):
            stopwords = _SEARCH_STOPWORDS | _LARGE_CORPUS_SEARCH_STOPWORDS

        q_terms = _search_tokens(task, stopwords)             # 原始词：子串/标签检查用
        q_tokens = _expand_query_terms(q_terms, _auto_domains(task))  # 扩展家族：BM25 用
        if not q_terms:
            return []
        q_set = set(q_terms)

        docs = [_tokenize_list(f"{e.get('content', '')} {e.get('category', '')} "
                               f"{' '.join(e.get('tags', []))}") for e, _ in pairs]
        idf = _compute_idf(docs)
        avgdl = _compute_avgdl(docs)
        task_domains = _auto_domains(task)

        scored = []
        for i, (e, label) in enumerate(pairs):
            content = e.get("content", "")
            content_low = content.lower()

            bm25 = _bm25_score(q_tokens, docs[i], idf, avgdl)
            sub = 2.0 if q_low in content_low else \
                (1.0 if any(t in content_low for t in q_set) else 0.0)
            tags_low = [t.lower() for t in e.get("tags", [])]
            tag = 5.0 if q_low in tags_low else \
                (1.5 if any(q_low in t for t in tags_low) else 0.0)
            if q_low in e.get("category", "").lower():
                tag += 1.0
            match_score = bm25 + sub + tag
            if match_score <= 0:
                continue                                      # 一路信号都没有：与任务无关

            e_domains = e.get("domains")
            if e_domains is None:
                e_domains = _auto_domains(content)
                e["domains"] = e_domains
            dom_score = 0.0
            if task_domains and e_domains:
                inter = set(task_domains) & set(e_domains)
                union = set(task_domains) | set(e_domains)
                dom_score = len(inter) / len(union) if union else 0.0

            score = match_score * 0.7 + dom_score * 0.3       # BM25 主导，域轻推
            score += math.log1p(e.get("usage_count", 0)) * 0.3   # 反馈兑现成排名
            age_d = (now - e.get("updated_at", now)) / 86400.0
            score += 0.5 / (1.0 + age_d)                      # 新近加权（平滑衰减）

            scored.append((score, e, label))

        if not scored:
            return []
        best = max(s for s, _, _ in scored)
        scored = [(s, e, lb) for s, e, lb in scored if s >= best * GATE_SCORE]
        scored.sort(key=lambda x: (-x[0], -x[1].get("usage_count", 0)))
        return [(e, lb) for _, e, lb in scored[:top_k]]

    def search(self, task: str, top_k: int = TOP_K) -> list:
        """检索：跨作用域同一语料打分（BM25 全局 IDF），返回条目列表"""
        return [e for e, _ in self._search_ranked(task, top_k)]

    # ── 注入（压力自适应剂量：斜坡替代悬崖）──
    def injection_block(self, task: str, context_usage: float) -> tuple:
        """返回 (记忆块文本, 注入id列表)。剂量随上下文压力自适应收缩
        （斜坡替代悬崖：<0.4 全额3张 → 0.4~0.6 两张 → 0.6~0.75 一张 → ≥0.75 关门；
        限流思想收编自 MiniCode 自适应冷却，但不碰注入位置、缓存前缀零损伤）"""
        top_k = adaptive_top_k(context_usage)
        if top_k <= 0:
            return "", []                                 # 高压关门
        ranked = self._search_ranked(task, top_k)
        if not ranked:
            return "", []
        lines = ["<记忆上下文>"]
        ids = []
        for i, (e, label) in enumerate(ranked, 1):
            lines.append(f"{i}. [{label}][{e.get('category', '')}] "
                         f"{e.get('content', '')[:CAP_CHARS]}")
            ids.append(e["id"])
            e["last_accessed"] = time.time()     # 被检索也算一种使用
        lines.append("</记忆上下文>")
        self._save_all()
        return "\n".join(lines), ids

    # ── 反馈 ──
    def feedback(self, success: bool, ids: list):
        """结算在场卡：成功全体+2，失败全体−1（floor 0）。
        场外不结算；id 可能住在任一作用域，跨库查找。"""
        changed = False
        now = time.time()
        for store in self._stores():
            for e in store.entries:
                if e["id"] in ids:
                    e["usage_count"] = max(0, e["usage_count"] + (2 if success else -1))
                    e["last_accessed"] = now
                    changed = True
        if changed:
            self._save_all()

    # ── 写入时冲突检测 ──
    def detect_conflicts(self, content: str, threshold: float = 0.35) -> list:
        """写入时冲突检测（抄 MiniCode detect_conflicts）：按 Jaccard≥threshold
        找出与新内容同主题的旧记忆。Jaccard 只能识别"主题重叠"，分不出矛盾方向
        ——所以只报告不拦截，把复核决定权留给策展人。
        阈值 0.35 是 Pcode 适配（MiniCode 默认 0.6）：它的英文按词分词，同主题
        对 easily 0.6+；中文 bigram 会稀释 Jaccard——实测同主题卡对只有 ~0.37，
        0.6 在中文场景永远不触发。跨作用域扫描。"""
        new_tokens = _tokenize(content)
        if not new_tokens:
            return []
        conflicts = []
        for store in self._stores():
            for e in store.entries:
                old_tokens = _tokenize(f"{e.get('content', '')} {e.get('category', '')} "
                                       f"{' '.join(e.get('tags', []))}")
                union = new_tokens | old_tokens
                sim = len(new_tokens & old_tokens) / len(union) if union else 0.0
                if sim >= threshold:
                    conflicts.append((e, round(sim, 3)))
        conflicts.sort(key=lambda x: -x[1])
        return conflicts

    # ── 策展人（两个库一起策：去重/验尸/衰减/升降级/熬洞见）──
    def maintain(self, llm=None) -> dict | None:
        """未到周期返回 None，到了返回合并报告。
        计数跨会话持久（存在各库 memory.json 的 task_count 里，取最大值）"""
        self.task_count += 1
        if self.task_count < MAINTAIN_INTERVAL:
            self._save_all()     # 计数也必须落盘：否则一段无产出的会话退出即丢计数
            return None
        self.task_count = 0
        dedup = stale = decay = insights = states_evicted = 0
        tiers = {"promoted": 0, "archived": 0, "reactivated": 0}
        for store in self._stores():
            dedup += store._dedup()
            stale += store._validate_stale()
            decay += store.decay_memories()
            states_evicted += store.decay_states()
            t = store.promote_memories()
            for k in tiers:
                tiers[k] += t[k]
            insights += store._consolidate(llm) if llm else 0
        for store in self._stores():
            store._prune()
            store._prune_states()
            store._save()
        return {"dedup": dedup, "stale": stale, "decay": decay,
                "states": states_evicted, "tiers": tiers, "insights": insights}


# ── 压力自适应剂量（限流思想收编自 MiniCode 自适应冷却，斜坡替代悬崖）──
def adaptive_top_k(pressure: float, full: int = TOP_K) -> int:
    """上下文越满，注入张数越少——不碰注入位置，缓存前缀零损伤。
    <0.4 宽裕→全额；0.4~0.6→减一；0.6~0.75→1张；≥0.75 关门（原高压闸）。"""
    if pressure >= 0.75:
        return 0
    if pressure >= 0.6:
        return 1
    if pressure >= 0.4:
        return max(1, full - 1)
    return full
