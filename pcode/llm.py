"""模型调用封装：OpenAI 兼容接口（DashScope/DeepSeek/GLM 通用）"""
from openai import OpenAI


class LLM:
    def __init__(self, model: str, api_key: str, base_url: str, temperature: float = 0.1):
        self.model = model
        self.temperature = temperature
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, messages: list, tools: list | None = None):
        """发送对话，返回原始 message 对象（带 tool_calls 或 content）"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            tools=tools,          # None 时 API 自动忽略
        )
        return response.choices[0].message
