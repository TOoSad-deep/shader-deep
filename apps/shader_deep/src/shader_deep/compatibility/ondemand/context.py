"""旧按需整合会话的上下文, 不进入当前默认角色."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.library.models import compact_data
from shader_deep.infrastructure.llm.messages import _message

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage

    from shader_deep.domain.library.models import VisualOutline


def build_integration_context(
    user_request: str,
    outline: VisualOutline,
    reference_url: str,
    *,
    index: object | None = None,
    control: dict[str, object] | None = None,
) -> HumanMessage:
    """整合时提供轻量目录和当前比较正文, 初稿元素不重复展开.

    Args:
        user_request: 用户原始要求.
        outline: 当前统一视觉初稿.
        reference_url: 完整原始图像数据 URL.
        index: 后端提供的目录、当前比较、必要工作与问题进度.
        control: 当前需要处理的简短执行反馈.

    Returns:
        包含轻量发现入口及本组显式选材的整合输入.
    """
    outline_data = compact_data(outline)
    comparison = index.get("comparison") if isinstance(index, dict) else None
    if isinstance(comparison, dict) and isinstance(outline_data, dict):
        selected = {entry["handle"] for entry in comparison.get("entries", []) if entry.get("kind") == "element"}
        outline_data["elements"] = [element for element in outline_data.get("elements", []) if element["id"] not in selected]
    payload: dict[str, object] = {"user_request": user_request, "visual_outline": outline_data}
    if index is not None:
        payload["integration_materials"] = index
    return _message(payload, reference_url, control)
