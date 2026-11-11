"""扩展工具层（Utility Profile）：低频/重依赖的工具住这里。

与核心工具层的唯一区别是**加载策略**而非结构——同样是 @tool 装饰的普通工具：
- core 档：本模块根本不被 import（连 requests 库的导入成本都不付）
- full 档：由 tools.create_tool_collection(profile="full") 懒导入并注册
（分层思想照抄 MiniCode 的 tools/http_utils.py + toolProfile 机制）
"""
import json

try:
    import requests
except ImportError:              # 未装requests不炸模块，调用时给出提示
    requests = None

from pcode.tool import tool

HTTP_TIMEOUT = 30              # 秒
MAX_HTTP_BODY_CHARS = 20_000   # 源头限量：响应体截断（同run_command的哲学——新鲜豁免拦不住源头）
_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")
# 响应头只挑对调试有用的几个（www-authenticate在401时会告诉你该用什么认证方式）
_INTERESTING_HEADERS = ("content-type", "content-length", "location",
                        "www-authenticate", "retry-after", "server")


@tool
def http_request(url: str, method: str = "GET", headers: str = "",
                 params: str = "", body: str = "") -> str:
    """向指定 URL 发送 HTTP 请求，返回 状态码 + 关键响应头 + 响应体。
    headers/params 传 JSON 字符串，如 '{"Authorization": "Bearer x"}'；
    body 是请求体原文（发JSON体请自行加 Content-Type: application/json 头）。
    超时30秒；响应体超过20000字符会截断。"""
    if requests is None:
        return "错误: 未安装 requests 库（pip install requests）"

    method = method.strip().upper()
    if method not in _HTTP_METHODS:
        return f"错误: 不支持的请求方法 {method}（可用 {'/'.join(_HTTP_METHODS)}）"
    try:
        hdrs = json.loads(headers) if headers else None
        prms = json.loads(params) if params else None
    except json.JSONDecodeError as e:
        return f"错误: headers/params 必须是合法的 JSON 字符串: {e}"

    try:
        resp = requests.request(
            method, url, headers=hdrs, params=prms,
            data=body if body else None,
            timeout=HTTP_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return f"错误: 请求超时（>{HTTP_TIMEOUT}秒）: {url}"
    except requests.exceptions.RequestException as e:
        return f"错误: 请求失败: {e}"

    head = "; ".join(f"{k}: {v}" for k, v in resp.headers.items()
                     if k.lower() in _INTERESTING_HEADERS)

    body_text = resp.text
    note = ""
    if len(body_text) > MAX_HTTP_BODY_CHARS:
        body_text = body_text[:MAX_HTTP_BODY_CHARS]
        note = f"\n\n[响应体超长，已截断至 {MAX_HTTP_BODY_CHARS} 字符]"

    parts = [f"状态码: {resp.status_code} {resp.reason}"]
    if head:
        parts.append(f"响应头: {head}")
    # 第3层审查（prompt注入防御）：外部响应体是数据不是指令，围栏+防御语包住再进上下文
    if body_text:
        from pcode.permissions import mark_external
        body_text = mark_external(url, body_text) + note
    parts.append(f"响应体:\n{body_text or '（空）'}")
    return "\n".join(parts)


EXTENSION_TOOLS = [http_request]     # 扩展层清单：profile=full 时整体注册
