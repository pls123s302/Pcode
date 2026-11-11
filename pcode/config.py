"""配置加载：~/.pcode/settings.json > 环境变量
（优先级链设计照搬 MiniCode：身份配置住用户主目录，不进代码、不进git）
"""
import json
import os
from pathlib import Path

SETTINGS_PATH = Path.home() / ".pcode" / "settings.json"

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


def load_settings() -> dict:
    """按优先级合并配置：settings.json 优先，环境变量兜底"""
    file_cfg = {}
    if SETTINGS_PATH.exists():
        # utf-8-sig: 容忍 BOM 字节（MiniCode 在这里翻过车，我们不翻）
        text = SETTINGS_PATH.read_text(encoding="utf-8-sig")
        file_cfg = json.loads(text)

    def pick(file_key: str, env_key: str, default: str = "") -> str:
        value = file_cfg.get(file_key) or os.environ.get(env_key, "")
        return str(value).strip() or default

    return {
        "model": pick("model", "PCODE_MODEL", DEFAULT_MODEL),
        "api_key": pick("api_key", "DASHSCOPE_API_KEY"),
        "base_url": pick("base_url", "PCODE_BASE_URL", DEFAULT_BASE_URL),
        "tool_profile": pick("tool_profile", "PCODE_TOOL_PROFILE", "core"),
    }
