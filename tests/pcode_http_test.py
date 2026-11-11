# -*- coding: utf-8 -*-
"""http_request + profile 机制离线验证：本地真HTTP服务器，不依赖外网"""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pcode.tools_ext import http_request
from pcode.tools import create_tool_collection_profiled

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{'✅' if cond else '❌'} {name}")

# ── 本地测试服务器 ─────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        seen = self.headers.get("X-Test", "")
        self._reply(200, f'{{"ok": true, "path": "{self.path}", "seen": "{seen}"}}'.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self._reply(201, f'{{"created": true, "ct": "{self.headers.get("Content-Type", "")}", '
                         f'"echo_len": {len(body)}}}'.encode())

    def log_message(self, *a):    # 静默访问日志
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

print("== 一、http_request 行为 ==")
r = http_request(url=f"{base}/hello?x=1")
check("GET: 状态码200 + 响应体", "状态码: 200 OK" in r and '"ok": true' in r)
check("GET: params查询串生效", "/hello?x=1" in r)

r = http_request(url=f"{base}/auth", headers='{"X-Test": "abc"}')
check("GET: headers以JSON传入并送达", '"seen": "abc"' in r)

r = http_request(url=f"{base}/create", method="POST",
                 headers='{"Content-Type": "application/json"}',
                 body='{"name": "pcode"}')
check("POST: 201 + 请求体送达", "状态码: 201 Created" in r
      and '"echo_len": 17' in r and '"ct": "application/json"' in r)

r = http_request(url=f"{base}/x", method="BREW")
check("非法方法被拦截", r.startswith("错误: 不支持的请求方法"))

r = http_request(url=f"{base}/x", headers="不是json")
check("非法headers被拦截并提示", r.startswith("错误: headers/params 必须是合法的 JSON"))

r = http_request(url="http://127.0.0.1:1/nope")   # 无人监听的端口 → 连接失败
check("连接失败返回友好错误", r.startswith("错误: 请求失败"))

print("== 二、profile 机制 ==")
core = create_tool_collection_profiled("core")
full = create_tool_collection_profiled("full")
util = create_tool_collection_profiled("utility")
CORE6 = {"list_files", "read_file", "write_file", "edit_file", "run_command", "file_tree"}
check("core档: 恰好6个常驻工具", set(core.tools) == CORE6)
check("full档: 7个含http_request",
      set(full.tools) == CORE6 | {"http_request"})
check("utility是full的别名", set(util.tools) == set(full.tools))
check("core是full的严格子集", set(core.tools) < set(full.tools))

# 懒加载实证：子进程里只 import pcode.tools（core路径），requests 不应被加载
probe = ("import sys; sys.path.insert(0, r'C:\\Pycharm_Project_item\\Pcode'); "
         "import pcode.tools; print('requests' in sys.modules)")
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
check("懒加载实证: core档下requests未被导入", out.stdout.strip() == "False")

print()
fails = [n for n, ok in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(fails)} 项"
      + (f"，失败：{fails}" if fails else "，全部通过 🎉"))
sys.exit(1 if fails else 0)
