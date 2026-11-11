"""会话持久化：主文件 + 增量目录的混合保存（复刻 MiniCode 三层策略，修正其计数bug）

行为规则（与 MiniCode 对齐）：
- 启动默认开新会话；恢复必须显式 /resume latest 或 /resume <id>
- /sessions 只列出当前工作区的会话（workspace 精确匹配）
- /resume <id> 允许跨工作区恢复，不检查——文件安全由权限层兜底

保存策略（增量 + 定期全量，把"勤保存"的成本从 O(总量) 降到 O(增量)）：
- 首次保存 = 全量快照（sessions/<id>.json，含两本账全部内容）
- 平时保存 = 增量片（sessions/deltas/<id>/delta_XXXX.json，只写上次之后的新增，
  每片自带偏移量"我从第几条续"）
- 攒满 _FULL_SAVE_INTERVAL 个增量（或触及安全帽）→ 全量保存并清空增量目录
- messages 是可变账本（压缩会改写它）→ 改写后必须 force_full，
  增量偏移量对"中间被改写的数据"无意义
加载 = 读主文件 + 按编号回放增量片，偏移量做幂等闸门：
  片偏移 >= 当前长度 → 应用（新货）；< 当前长度 → 已吸收，跳过（旧货）。
  任何时刻崩溃（主文件写完没删增量 / 主文件没写成），加载都收敛到同一状态：不丢不重。
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pcode" / "sessions"
DELTA_DIR_NAME = "deltas"
_FULL_SAVE_INTERVAL = 10    # 每攒10个增量片做一次全量合并
_MAX_DELTA_FILES = 50       # 安全帽：增量片到50强制全量，防目录无限膨胀
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # 防路径穿越：id 只允许安全字符


def _json_default(o):
    """兜底：漏网的SDK对象转dict，其他转字符串——存盘永不因类型炸"""
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return str(o)


def _atomic_write_json(path: Path, data) -> None:
    """原子落盘：临时文件 + os.replace，任何时刻被杀文件都是完整旧版或完整新版"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix("." + uuid.uuid4().hex[:6] + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_json_default),
                   encoding="utf-8")
    os.replace(tmp, path)


def _delta_dir(session_id: str) -> Path:
    return SESSIONS_DIR / DELTA_DIR_NAME / session_id


def _to_store(msg):
    """SDK 消息对象 → dict（openai SDK 是 pydantic 的），dict 原样保留"""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    return {"role": "assistant", "content": str(msg)}


class Session:
    """一段连续对话：共享一份 messages 历史的回合序列"""

    def __init__(self, workspace: str, session_id: str | None = None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.workspace = str(Path(workspace).resolve())   # 归一化存储，比较才有意义
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.title = ""                                    # 第一条用户消息前30字
        self.messages = []                                 # 工作账本：可被压缩改写
        self.transcript = []                               # 流水账本：append-only，永不压缩
        # ── 增量保存记账 ──
        self._saved_msg = 0                                # 已落盘的 messages 条数
        self._saved_trans = 0                              # 已落盘的 transcript 条数
        self._delta_count = 0                              # 本次全量以来写了几个增量片
        self._has_full = False                             # 是否已有全量基底（首次保存必须全量）

    # ── 账本操作 ────────────────────────────────────────────
    def append(self, message):
        msg = _to_store(message)
        self.messages.append(msg)
        self.updated_at = time.time()
        if not self.title and msg.get("role") == "user":
            self.title = (msg.get("content") or "")[:30]

    def append_transcript(self, kind: str, **data):
        """流水账：完整记录（含工具全文），压缩永不碰它——/replay 和反思的底气"""
        entry = {"kind": kind, "ts": time.time()}
        entry.update(data)
        self.transcript.append(entry)
        self.updated_at = time.time()

    def get(self) -> list:
        return self.messages

    def sync_from_runtime(self, messages, transcript, force_full: bool = False):
        """边界同步：运行世界 → 持久世界（两本账整体替换）。

        两世界架构的唯一接口——运行期间持久层冻结（压缩随便改运行态，
        偏移量毫无感知），只在会话边界（任务结束/异常/退出）走这里。
        改写检测：delta 只对"纯追加"有效。若运行态的前缀与已落盘前缀
        不是同一批对象（压缩重写过/头部插过队），或长度缩水，增量偏移量
        已作废 → 强制全量。身份校验比"比长度"可靠：压缩后再长回去，
        长度上看不出被动过，对象身份看得出来。
        """
        n = self._saved_msg
        rewritten = len(messages) < n or any(
            a is not b for a, b in zip(self.messages[:n], messages[:n]))
        self.messages = list(messages)
        self.transcript = list(transcript)
        if not self.title:                       # 运行期不再走append，标题在这里补
            for m in self.messages:
                if m.get("role") == "user":
                    self.title = str(m.get("content") or "")[:30]
                    break
        self.save(force_full=force_full or rewritten)

    # ── 混合保存 ────────────────────────────────────────────
    def save(self, force_full: bool = False):
        """该全量时全量（首存/攒满/安全帽/强制），平时走增量（KB级，O(新增)）"""
        if force_full or not self._has_full \
                or self._delta_count >= _FULL_SAVE_INTERVAL \
                or self._delta_count >= _MAX_DELTA_FILES:
            self._save_full()
        else:
            self._save_delta()

    def _save_full(self):
        self.updated_at = time.time()
        data = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace": self.workspace,
            "title": self.title,
            "messages": self.messages,
            "transcript": self.transcript,
        }
        _atomic_write_json(SESSIONS_DIR / f"{self.session_id}.json", data)
        self._has_full = True
        self._saved_msg = len(self.messages)
        self._saved_trans = len(self.transcript)
        self._delta_count = 0
        self._clear_deltas()

    def _save_delta(self):
        new_msgs = self.messages[self._saved_msg:]
        new_trans = self.transcript[self._saved_trans:]
        if not new_msgs and not new_trans:
            return                                  # 没有新货，不产生空片
        d = _delta_dir(self.session_id)
        d.mkdir(parents=True, exist_ok=True)
        self._delta_count += 1
        data = {"ts": time.time(),
                "msg_offset": self._saved_msg,
                "transcript_offset": self._saved_trans}
        if new_msgs:
            data["messages"] = new_msgs
        if new_trans:
            data["transcript"] = new_trans
        _atomic_write_json(d / f"delta_{self._delta_count:04d}.json", data)
        self._saved_msg = len(self.messages)
        self._saved_trans = len(self.transcript)

    def _clear_deltas(self):
        """全量已吸收全部内容，增量片的历史使命完成——删除（信息在主文件里）"""
        d = _delta_dir(self.session_id)
        if not d.is_dir():
            return
        for f in d.glob("delta_*.json"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            d.rmdir()                               # 空了就连目录一起收走
        except OSError:
            pass


# ── 加载（主文件 + 回放增量，偏移量幂等）────────────────────
def load_session(session_id: str) -> Session | None:
    """按 id 加载。无工作区检查——跨工作区恢复是合法操作"""
    if not _SAFE_ID.match(session_id):    # "../etc" 之类的id直接拒绝
        return None
    f = SESSIONS_DIR / f"{session_id}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    s = Session(workspace=data["workspace"], session_id=data["session_id"])
    s.created_at = data.get("created_at", time.time())
    s.updated_at = data.get("updated_at", time.time())
    s.title = data.get("title", "")
    s.messages = data.get("messages", [])
    s.transcript = data.get("transcript", [])

    # 回放增量片：偏移量 >= 当前长度 → 是新货，应用；否则已被主文件吸收，跳过
    d = _delta_dir(session_id)
    if d.is_dir():
        applied = 0
        for df in sorted(d.glob("delta_*.json")):
            try:
                dd = json.loads(df.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                continue                     # 单片损坏跳过，绝不炸整个恢复
            if "messages" in dd:
                off = dd.get("msg_offset", len(s.messages))
                if off >= len(s.messages):
                    s.messages.extend(dd["messages"])
            if "transcript" in dd:
                off = dd.get("transcript_offset", len(s.transcript))
                if off >= len(s.transcript):
                    s.transcript.extend(dd["transcript"])
            applied += 1
        s._delta_count = applied             # 延续全量节奏（再攒10片才合并）

    # 记账对齐：主文件+已回放的增量 = 当前已知落盘进度
    s._saved_msg = len(s.messages)
    s._saved_trans = len(s.transcript)
    s._has_full = True
    return s


def list_sessions(workspace: str | None = None) -> list:
    """列出会话元数据（按更新时间倒序）。传 workspace 则精确过滤。
    计数和时间合并增量尾巴——否则主文件未合并时显示的是旧数据"""
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if workspace is not None and data.get("workspace") != workspace:
            continue
        msg_count = len(data.get("messages", []))
        updated = data.get("updated_at", 0)
        d = _delta_dir(f.stem)
        if d.is_dir():
            for df in sorted(d.glob("delta_*.json")):
                try:
                    dd = json.loads(df.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, OSError):
                    continue
                msg_count += len(dd.get("messages", []))
                updated = max(updated, dd.get("ts", 0))
        out.append({
            "session_id": data.get("session_id", f.stem),
            "title": data.get("title", ""),
            "updated_at": updated,
            "message_count": msg_count,
        })
    return sorted(out, key=lambda m: m["updated_at"], reverse=True)


def get_latest_session(workspace: str | None = None) -> Session | None:
    metas = list_sessions(workspace=workspace)
    return load_session(metas[0]["session_id"]) if metas else None
