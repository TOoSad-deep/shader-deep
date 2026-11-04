"""工具共用的图片消息格式与错误反馈设置, 不持有会话状态."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.rendering import PreviewSnapshot


def configure_tool(operation: BaseTool) -> BaseTool:
    """将预期工具错误和入参错误返回为模型可读的错误消息."""
    operation.handle_tool_error = True
    operation.handle_validation_error = True
    return operation


def image_content(snapshot: PreviewSnapshot, *, trial_id: int | None = None, comparison: bytes | None = None) -> list[dict[str, object]]:
    """将同一快照的元数据和图片包装为多模态消息.

    Args:
        snapshot: 参数与 PNG 来自同一次 capture 的只读快照.
        trial_id: 已成功记录的截图编号, 无历史时不提供.
        comparison: 可选并排图, 不提供时发送原始 PNG.

    Returns:
        元数据文本块和实际图片块, 不包含评价指标.
    """
    # 元数据和 PNG 必须来自同一次 capture, 不能用更新时的旧参数描述新图.
    metadata: dict[str, object] = {
        "revision": snapshot.revision,
        "parameters": dict(snapshot.parameters),
        "width": snapshot.width,
        "height": snapshot.height,
        "time": snapshot.time,
    }
    if trial_id is not None:
        metadata["trial_id"] = trial_id
    if comparison is not None:
        metadata["layout"] = "reference_left_current_right"
        metadata["image_width"] = snapshot.width * 2
        metadata["image_height"] = snapshot.height + 24
    png = snapshot.png if comparison is None else comparison
    return [
        {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
    ]
