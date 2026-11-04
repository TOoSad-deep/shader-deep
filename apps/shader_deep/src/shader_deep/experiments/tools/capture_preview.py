"""只获取当前原始截图; 配置历史时记录同一次快照."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.tools import ToolException

from shader_deep.experiments.tools._common import configure_tool, image_content

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.experiments._parameter_history import PreviewHistory
    from shader_deep.rendering import ShaderPreview


def build_tool(preview: ShaderPreview, *, history: PreviewHistory | None = None) -> BaseTool:
    """绑定原图工具; 不传历史时保持原来的无编号输出.

    Args:
        preview: 调用方管理的常驻预览.
        history: 可选的本次工具绑定历史, 与对比工具共享同一对象.

    Returns:
        返回当前原始 PNG 与快照元数据的工具.
    """

    @tool
    def capture_preview() -> list[dict[str, object]]:
        """获取当前画布 PNG、完整参数、版本和时间, 用于观察调参效果."""
        try:
            snapshot = preview.capture()
            return image_content(snapshot, trial_id=None if history is None else history.record(snapshot))
        except ValueError as exc:
            raise ToolException(str(exc)) from exc

    return configure_tool(capture_preview)
