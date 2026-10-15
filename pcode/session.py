"""会话持久化：一个会话一个 JSON 文件（复刻 MiniCode 的存储与行为规则）

行为规则（与 MiniCode 对齐）：
- 启动默认开新会话；恢复必须显式 /resume latest 或 /resume <id>
- /sessions 只列出当前工作区的会话（workspace 精确匹配）
- /resume <id> 允许跨工作区恢复，不检查——文件安全由权限层的工作区边界兜底
"""
import json
import re
import time
import uuid
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pcode" / "sessions"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # 防路径穿越：id 只允许安全字符


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
        self.messages = []                                 # 工作账本：模型读的上下文，可被压缩改写
        self.transcript = []                               # 流水账本：append-only 全程记录，永不压缩

    def append(self, message):
        msg = _to_store(message)
        self.messages.append(msg)
        self.updated_at = time.time()
        if not self.title and msg.get("role") == "user":
            self.title = (msg.get("content") or "")[:30]

    def append_transcript(self, kind: str, **data):
        """流水账：完整记录（含工具全文），压缩永不碰它——/replay 和审计的底气"""
        entry = {"kind": kind, "ts": time.time()}
        entry.update(data)
        self.transcript.append(entry)
        self.updated_at = time.time()

    def get(self) -> list:
        return self.messages

    def save(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        def _json_default(o):
            """兜底：漏网的SDK对象转dict，其他转字符串——存盘永不因类型炸"""
            if hasattr(o, "model_dump"):
                return o.model_dump()
            return str(o)

        data = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": time.time(),
            "workspace": self.workspace,
            "title": self.title,
            "messages": self.messages,
            "transcript": self.transcript,
        }
        (SESSIONS_DIR / f"{self.session_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1, default=_json_default),
            encoding="utf-8",
        )


def load_session(session_id: str) -> Session | None:
    """按 id 加载。无工作区检查——跨工作区恢复是合法操作"""
    if not _SAFE_ID.match(session_id):    # "../etc" 之类的id直接拒绝
        return None
    f = SESSIONS_DIR / f"{session_id}.json"
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8-sig"))
    s = Session(workspace=data["workspace"], session_id=data["session_id"])
    s.created_at = data.get("created_at", time.time())
    s.updated_at = data.get("updated_at", time.time())
    s.title = data.get("title", "")
    s.messages = data.get("messages", [])
    s.transcript = data.get("transcript", [])
    return s


def list_sessions(workspace: str | None = None) -> list:
    """列出会话元数据（按更新时间倒序）。传 workspace 则精确过滤"""
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
        out.append({
            "session_id": data.get("session_id", f.stem),
            "title": data.get("title", ""),
            "updated_at": data.get("updated_at", 0),
            "message_count": len(data.get("messages", [])),
        })
    return sorted(out, key=lambda m: m["updated_at"], reverse=True)


def get_latest_session(workspace: str | None = None) -> Session | None:
    metas = list_sessions(workspace=workspace)
    return load_session(metas[0]["session_id"]) if metas else None
