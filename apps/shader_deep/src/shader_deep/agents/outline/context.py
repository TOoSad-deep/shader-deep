"""初稿规划的模型输入."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.infrastructure.llm.messages import _message

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage


def build_outline_context(user_request: str, reference_url: str) -> HumanMessage:
    """首次观察只提供用户原始要求和完整图像.

    Args:
        user_request: 未混入模型结论的用户要求.
        reference_url: 本次运行固定的原始图像数据 URL.

    Returns:
        不含运行记录或探索方向的多模态消息.
    """
    return _message({"user_request": user_request}, reference_url)
