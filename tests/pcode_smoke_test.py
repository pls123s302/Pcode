# -*- coding: utf-8 -*-
"""真实API冒烟：1次调用验证新agent整条链（session=None，不产生会话文件）"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pcode.config import load_settings
from pcode.llm import LLM
from pcode.agent import Agent
from pcode.tools import create_tool_collection_profiled

cfg = load_settings()
llm = LLM(model=cfg["model"], api_key=cfg["api_key"], base_url=cfg["base_url"])
agent = Agent(llm=llm, tools=create_tool_collection_profiled())
print("回答:", agent.run("直接回答：1+1等于几？不要使用任何工具。"))
