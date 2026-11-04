"""装配调参实验工具, 只在这里读取参考图并创建共享历史."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from shader_deep.experiments._parameter_history import PreviewHistory
from shader_deep.experiments.tools.capture_preview import build_tool as build_capture_tool
from shader_deep.experiments.tools.compare_preview import build_tool as build_compare_tool
from shader_deep.experiments.tools.get_trial_history import build_tool as build_history_tool
from shader_deep.experiments.tools.set_parameters import build_tool as build_update_tool
from shader_deep.imaging.comparison import side_by_side_png
from shader_deep.infrastructure.llm.messages import png_data_url

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.tools import BaseTool

    from shader_deep.rendering import ShaderPreview


def build_parameter_tools(preview: ShaderPreview, *, reference_path: Path | None = None) -> list[BaseTool]:
    """为同一个预览会话装配工具, 不接管浏览器生命周期.

    Args:
        preview: 由调用方在 with 内管理的 ShaderPreview, 工具不会关闭它.
        reference_path: 可选参考 PNG, 构建时固定字节, 不把路径发送给模型.

    Returns:
        按 set_parameters、capture_preview 顺序排列的工具; 有参考图时
        追加 compare_preview、get_trial_history, 共用本次绑定的成功截图历史.

    Raises:
        ValueError: 参考文件不存在、扩展名不符或内容不是可解码 PNG.
        OSError: 参考文件无法读取.
    """
    if reference_path is None:
        return [build_update_tool(preview), build_capture_tool(preview)]
    reference = base64.b64decode(png_data_url(reference_path).split(",", 1)[1])
    side_by_side_png(reference, reference)
    history = PreviewHistory()
    return [
        build_update_tool(preview),
        build_capture_tool(preview, history=history),
        build_compare_tool(preview, reference=reference, history=history),
        build_history_tool(history),
    ]
