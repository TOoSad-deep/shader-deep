"""OpenRouter 推理块的无损回传与流式汇集, 不改变工具调用正文."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit

from langchain_core.messages import AIMessage, BaseMessage

RAW_REASONING = "openrouter_reasoning_delta"
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_CHILDREN = {
    "items",
    "prefixItems",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}


def _tool_parameters(schema: object) -> object:
    """仅在外发 Schema 移除非空白正则, 本地 Text 校验继续执行.

    当前严格工具服务会把非空白查找正则约束成单字符. 保留长度和其他
    正则约束, 仅遍历 Schema 子节点, 不修改 default、enum 或 examples 数据.
    """
    if isinstance(schema, list):
        return [_tool_parameters(child) for child in schema]
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    # 只沿 JSON Schema 关键字递归; 示例数据里的同名 pattern 不属于待适配约束.
    for key, value in schema.items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: _tool_parameters(child) for name, child in value.items()}
        elif key in _SCHEMA_CHILDREN:
            result[key] = _tool_parameters(value)
    if schema.get("type") == "string" and schema.get("pattern") == r"\S":
        result.pop("pattern")
    return result


def is_openrouter(base_url: str | None) -> bool:
    """仅对 OpenRouter 域名启用网关特定载荷规则."""
    return urlsplit(base_url or "").hostname == "openrouter.ai"


def restore_reasoning(payload: dict, messages: list[BaseMessage]) -> None:
    """将工具调用之前的完整推理块附回对应 assistant 消息."""
    # 推理块是提供方回传协议的一部分, 按消息原位复制, 不改写为正文或混入其他角色.
    for encoded, message in zip(payload["messages"], messages, strict=True):
        if isinstance(message, AIMessage) and isinstance(message.additional_kwargs.get("reasoning_details"), list):
            encoded["reasoning_details"] = deepcopy(message.additional_kwargs["reasoning_details"])


class StreamTerminal:
    """重复结束帧只登记一次, 不接受相互冲突的结束原因."""

    def __init__(self) -> None:
        """记录当前请求的首个结束帧元数据."""
        self.metadata: dict = {}

    def accept(self, info: dict | None) -> bool:
        """去除重复终态元数据, 返回是否首次收到结束标记."""
        if not info or not info.get("finish_reason"):
            return False
        if not self.metadata:
            self.metadata = dict(info)
            return True
        if info["finish_reason"] != self.metadata["finish_reason"]:
            msg = "OpenRouter stream returned conflicting finish reasons"
            raise ValueError(msg)
        # 原地去掉重复字段, 后续框架合并 chunk 时便不会把同一结束原因拼接两次.
        for key, value in tuple(info.items()):
            if self.metadata.get(key) == value:
                del info[key]
        return False


class ReasoningBuffer:
    """每次流式请求独立汇集, 避免框架拼接重复的 type、id 等元数据."""

    def __init__(self) -> None:
        """建立当前请求的推理块索引."""
        self.blocks: dict[int, dict] = {}

    def append(self, details: list[dict]) -> None:
        """按块 index 合并文本增量, 保留签名与类型等稳定字段."""
        for position, detail in enumerate(details):
            index = detail.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int):
                msg = "OpenRouter reasoning block index must be an integer"
                raise TypeError(msg)
            block = self.blocks.setdefault(index, {})
            # 只有内容字段允许增量拼接; id、type、签名等元数据变化意味着流不一致.
            for key, value in detail.items():
                if key in {"text", "summary", "data"} and isinstance(value, str):
                    block[key] = block.get(key, "") + value
                elif key not in block or block[key] is None:
                    block[key] = deepcopy(value)
                elif value is not None and block[key] != value:
                    msg = f"OpenRouter reasoning block changed stable field: {key}"
                    raise ValueError(msg)

    def details(self) -> list[dict]:
        """返回按提供方块索引排序的完整原格式对象."""
        return [deepcopy(self.blocks[index]) for index in sorted(self.blocks)]
