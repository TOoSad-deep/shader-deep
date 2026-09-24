"""只向探索角色提供当前业务材料, 按需附加独立的控制反馈."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.messages import HumanMessage

from shader_deep.analysis.exploration import compact_data

if TYPE_CHECKING:
    from shader_deep.analysis.exploration import VisualOutline


def _message(payload: dict[str, object], reference_url: str, control: dict[str, object] | None = None) -> HumanMessage:
    """业务正文和真实原图保持固定, 控制信息不扩充业务协议."""
    content: list[str | dict[str, object]] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        {"type": "image_url", "image_url": {"url": reference_url}},
    ]
    if control:
        content.append({"type": "text", "text": "运行控制反馈: " + json.dumps(control, ensure_ascii=False, separators=(",", ":"))})
    return HumanMessage(content=content)


def build_outline_context(user_request: str, reference_url: str) -> HumanMessage:
    """首次观察只提供用户原始要求和完整图像.

    Args:
        user_request: 未混入模型结论的用户要求.
        reference_url: 本次运行固定的原始图像数据 URL.

    Returns:
        不含运行记录或探索方向的多模态消息.
    """
    return _message({"user_request": user_request}, reference_url)


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
