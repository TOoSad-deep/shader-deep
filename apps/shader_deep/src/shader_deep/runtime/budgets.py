"""在发送前约束完整请求估算量, 不把估算值当作服务端用量."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.utils.function_calling import convert_to_openai_tool

from shader_deep.runtime.execution import AnalysisLimitError
from shader_deep.runtime.usage import request_sizes

if TYPE_CHECKING:
    from langchain.agents.middleware import ModelRequest
    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage

    from shader_deep.runtime.options import RequestOptions


class RequestBudgetError(AnalysisLimitError):
    """完整请求不能装入显式配置的估算预算."""


def estimated_tokens(text_chars: int, images: int, options: RequestOptions) -> int:
    """按每个字符一个估算 token 并预留图像、输出与编码余量.

    Args:
        text_chars: 全部正文和工具结构的字符数.
        images: 实际图像块数量.
        options: 应用预算, 不推断提供方窗口.

    Returns:
        用于保守拒绝的估算量, 不用于计费报告.
    """
    return text_chars + images * options.request_image_tokens + options.max_output_tokens + options.request_token_margin


def remaining_material_chars(prompt: str, context: BaseMessage, tools: list[BaseTool], options: RequestOptions) -> int:
    """计算扣除固定材料后的正文预算, 完整请求仍会在发送前复核."""
    blocks = context.content if isinstance(context.content, list) else [context.content]
    images = sum(isinstance(block, dict) and block.get("type") == "image_url" for block in blocks)
    text = [block for block in blocks if not isinstance(block, dict) or block.get("type") != "image_url"]
    chars = len(prompt) + len(json.dumps(text, ensure_ascii=False))
    chars += len(json.dumps([convert_to_openai_tool(tool) for tool in tools], ensure_ascii=False))
    # 材料之外为决定和修复历史预留空间, 不能用装满窗口的包承诺后续局部修复.
    return max(1, options.max_context_tokens - estimated_tokens(chars, images, options) - options.integration_history_tokens)


def check_request(request: ModelRequest, tools: list[BaseTool], options: RequestOptions) -> None:
    """对包含历史、测量与工具结构的实际请求执行最后预算检查."""
    sizes = request_sizes(request, tools)
    chars = sizes["system_chars"] + sizes["tool_schema_chars"] + sizes["message_text_chars"]
    estimate = estimated_tokens(chars, sizes["image_count"], options)
    if estimate > options.max_context_tokens:
        msg = f"input_budget_exceeded: estimated total {estimate} exceeds configured {options.max_context_tokens}"
        raise RequestBudgetError(msg)
