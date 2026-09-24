"""记录实际请求的大小与服务用量, 不保存图像正文或把字符数伪装成 token."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.utils.function_calling import convert_to_openai_tool

if TYPE_CHECKING:
    from langchain.agents.middleware import ModelRequest
    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _blocks(message: BaseMessage) -> tuple[int, int]:
    calls = getattr(message, "tool_calls", None)
    arguments = _size(calls) if calls else 0
    if isinstance(message.content, str):
        return len(message.content) + arguments, 0
    texts, images = arguments, 0
    for block in message.content:
        if isinstance(block, str):
            texts += len(block)
        elif block.get("type") == "image_url":
            images += 1
        else:
            texts += _size(block)
    return texts, images


def request_sizes(request: ModelRequest, tools: list[BaseTool]) -> dict[str, int]:
    """统计准备发给模型的实际请求, 图片仅计数量.

    Args:
        request: 注入当前上下文后的模型请求.
        tools: 本角色实际开放的工具.

    Returns:
        字符与图片数量, 不作为服务端 token 计量.
    """
    counts = [_blocks(message) for message in request.messages]
    return {
        "system_chars": _size(request.system_message.content) if request.system_message else 0,
        "tool_schema_chars": _size([convert_to_openai_tool(tool) for tool in tools]),
        "message_text_chars": sum(text for text, _ in counts),
        "image_count": sum(images for _, images in counts),
        "message_count": len(request.messages),
        "tool_history_chars": sum(_blocks(message)[0] for message in request.messages if message.type == "tool"),
    }
