"""只向探索角色提供当前业务材料, 按需附加独立的控制反馈."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.library.models import compact_data
from shader_deep.infrastructure.llm.messages import _message

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage

    from shader_deep.domain.library.models import VisualOutline


def build_exploration_context(
    user_request: str,
    outline: VisualOutline,
    exploration_direction: str,
    reference_url: str,
    *,
    control: dict[str, object] | None = None,
) -> HumanMessage:
    """为每个独立子任务构造相同边界的三字段业务输入.

    Args:
        user_request: 用户原始要求.
        outline: 派发时固定的统一视觉初稿.
        exploration_direction: 当前任务的开放探索问题.
        reference_url: 完整原始图像数据 URL.
        control: 仅在需要修复或即将耗尽预算时提供的反馈.

    Returns:
        不包含其他任务报告、历史或来源目录的消息.
    """
    return _message(
        {"user_request": user_request, "visual_outline": compact_data(outline), "exploration_direction": exploration_direction},
        reference_url,
        control,
    )
