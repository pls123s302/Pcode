"""工具系统：@tool 装饰器 + 自动 schema + 注册表"""
import inspect

# Python 类型 → JSON Schema 类型
TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean"}


class Tool:
    """把一个普通函数包装成模型可调用的工具"""

    def __init__(self, func):
        self.func = func
        self.name = func.__name__
        self.description = func.__doc__   # docstring 就是给模型看的说明书

    def __call__(self, **kwargs):
        return self.func(**kwargs)

    def to_schema(self) -> dict:
        """从函数签名自动生成 OpenAI tools 格式的 schema"""
        properties = {}
        required = []
        for name, param in inspect.signature(self.func).parameters.items():
            param_type = TYPE_MAP.get(param.annotation)
            if param_type is None:
                raise TypeError(
                    f"工具 {self.name} 的参数 {name} 缺少类型注解，请写成 {name}: str 形式"
                )
            properties[name] = {"type": param_type, "description": name}
            if param.default is inspect.Parameter.empty:
                required.append(name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }


def tool(func):
    """装饰器：@tool 一行把函数变成 Tool 实例"""
    return Tool(func)


class ToolCollection:
    """工具注册表：对内是字典（分发执行），对外是 schema 列表（发给模型）"""

    def __init__(self):
        self.tools = {}

    def add(self, *tool_instances):
        for t in tool_instances:
            self.tools[t.name] = t
        return self                      # 支持链式写法

    def get(self, name: str):
        if name not in self.tools:
            raise KeyError(f"未注册的工具: {name}，可用: {list(self.tools)}")
        return self.tools[name]

    def to_openai_schemas(self) -> list:
        return [t.to_schema() for t in self.tools.values()]

    def describe(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" for t in self.tools.values())
