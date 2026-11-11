# -*- coding: utf-8 -*-
"""Pcode 混合保存（增量+定期全量）离线验证
覆盖：首存全量/增量片/加载合并/幂等闸门/周期合并/强制全量/计数含尾巴/旧格式兼容"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="pcode_delta_test_"))
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pcode.session as ps
ps.SESSIONS_DIR = ROOT / "sessions"          # 落盘改道临时目录

from pcode.session import Session, load_session, list_sessions

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

print("== 一、首存全量，再存增量 ==")
s = Session(workspace=str(ROOT))
s.append({"role": "user", "content": "第一个任务"})
s.append_transcript("user", text="第一个任务")
s.save()
main = ROOT / "sessions" / f"{s.session_id}.json"
check("首次保存=全量主文件", main.exists())
check("首次保存无增量目录", not (ROOT / "sessions" / "deltas" / s.session_id).exists())
check("首存后 _has_full=True", s._has_full and s._saved_msg == 1)

s.append({"role": "assistant", "content": "第一个回答"})
s.append_transcript("assistant", text="第一个回答")
s.append({"role": "user", "content": "第二个任务"})
s.append_transcript("user", text="第二个任务")
main_before = main.read_text(encoding="utf-8")
s.save()
delta_files = sorted((ROOT / "sessions" / "deltas" / s.session_id).glob("delta_*.json"))
check("二次保存=增量片", len(delta_files) == 1 and s._delta_count == 1)
check("主文件未被触碰（增量期间冻结）", main.read_text(encoding="utf-8") == main_before)

print("== 二、加载 = 主文件 + 回放增量 ==")
s2 = load_session(s.session_id)
check("加载: messages完整(基底+增量)", len(s2.messages) == 3)
check("加载: transcript完整", len(s2.transcript) == 3)
check("加载: 记账对齐(续写不重不漏)",
      s2._saved_msg == 3 and s2._saved_trans == 3 and s2._delta_count == 1)

print("== 三、幂等闸门（崩溃窗口不丢不重）==")
# 模拟"主文件已含全部、增量片还没删"的崩溃窗口：手工造一片过期的增量
stale = {"ts": 1.0, "msg_offset": 0, "transcript_offset": 0,
         "messages": [{"role": "user", "content": "过期重复条目"}],
         "transcript": [{"kind": "user", "ts": 1.0, "text": "过期重复条目"}]}
(ROOT / "sessions" / "deltas" / s.session_id / "delta_0099.json").write_text(
    json.dumps(stale, ensure_ascii=False), encoding="utf-8")
s3 = load_session(s.session_id)
check("过期增量被跳过（偏移量<当前长度）",
      len(s3.messages) == 3 and not any("过期" in str(m) for m in s3.messages))

print("== 四、攒满10片触发全量合并 ==")
s4 = Session(workspace=str(ROOT))
s4.save()                                     # 首存全量（空账本基底）
for i in range(11):                           # 11次"任务结束"保存：10次增量+1次触发全量
    s4.append({"role": "user", "content": f"任务{i}"})
    s4.append_transcript("user", text=f"任务{i}")
    s4.save()
leftover = list((ROOT / "sessions" / "deltas" / s4.session_id).glob("delta_*.json")) \
    if (ROOT / "sessions" / "deltas" / s4.session_id).exists() else []
check("攒满后增量目录被清空", leftover == [])
s5 = load_session(s4.session_id)
check("合并后主文件含全部11条", len(s5.messages) == 11 and s5.messages[-1]["content"] == "任务10")

print("== 五、force_full（压缩改写后的正确姿势）==")
s6 = Session(workspace=str(ROOT))
s6.append({"role": "user", "content": "长任务前的消息"})
s6.append_transcript("user", text="x")
s6.save()
s6.append({"role": "assistant", "content": "工作记录"})
s6.save()                                     # 增量
s6.messages[:] = [{"role": "user", "content": "[历史摘要] 压缩后的样子"}]   # 模拟压缩改写
s6.save(force_full=True)                      # 改写可变账本必须拍全量
check("force_full清空增量", not (ROOT / "sessions" / "deltas" / s6.session_id).exists())
s7 = load_session(s6.session_id)
check("改写后的可变账本被正确持久化", len(s7.messages) == 1
      and s7.messages[0]["content"].startswith("[历史摘要]"))

print("== 六、/sessions 计数含增量尾巴 ==")
s_cnt = Session(workspace=str(ROOT))          # 独立会话，避开第三节的过期片污染
s_cnt.append({"role": "user", "content": "第一"})
s_cnt.append_transcript("user", text="第一")
s_cnt.save()                                  # 全量：1条
s_cnt.append({"role": "assistant", "content": "第二"})
s_cnt.append({"role": "user", "content": "第三"})
s_cnt.save()                                  # 增量：2条
metas = list_sessions(workspace=str(ROOT))
mine = [m for m in metas if m["session_id"] == s_cnt.session_id]
check("计数合并了增量尾巴(3条而非1条)", mine and mine[0]["message_count"] == 3)

print("== 七、旧格式向后兼容 ==")
old = ROOT / "sessions" / "legacy00001.json"
old.write_text(json.dumps({
    "session_id": "legacy00001", "created_at": 1.0, "updated_at": 2.0,
    "workspace": str(ROOT), "title": "旧格式", "messages": [{"role": "user", "content": "老会话"}],
    "transcript": [],
}, ensure_ascii=False), encoding="utf-8")
s8 = load_session("legacy00001")
check("旧格式（无增量）加载正常", s8 is not None and s8.messages[0]["content"] == "老会话")

print("== 八、压缩后又长回去（长度没缩水，但历史被改写过）==")
s9 = Session(workspace=str(ROOT))
s9.sync_from_runtime([{"role": "user", "content": f"旧{i}"} for i in range(5)], [])
runtime = list(s9.get())
# 模拟一个任务内：先压缩成摘要，随后又新增到 8 条（比已落盘的 5 条还长）
runtime[:] = [{"role": "user", "content": "[历史摘要] 旧0~旧4"}] + \
             [{"role": "assistant", "content": f"新{i}"} for i in range(7)]
s9.sync_from_runtime(runtime, [])
s10 = load_session(s9.session_id)
check("改写后长回去：盘上状态与运行态一致（不混入旧历史、不丢新消息）",
      [m["content"] for m in s10.messages] == [m["content"] for m in runtime])

print("== 九、纯追加仍走增量（修复不能把增量废掉）==")
s11 = Session(workspace=str(ROOT))
s11.sync_from_runtime([{"role": "user", "content": "a"}], [])
rt = list(s11.get())
rt.append({"role": "assistant", "content": "b"})
s11.sync_from_runtime(rt, [])
check("纯追加 → 写的是增量片", s11._delta_count == 1)
check("纯追加 → 加载完整", len(load_session(s11.session_id).messages) == 2)

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项"
      + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
