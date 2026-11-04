"""只包装并排观察操作, 拼图交给 imaging, 历史交给独立记录对象."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.tools import ToolException

from shader_deep.experiments.tools._common import configure_tool, image_content
from shader_deep.imaging.comparison import side_by_side_png

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.experiments._parameter_history import PreviewHistory
    from shader_deep.rendering import ShaderPreview


def build_tool(preview: ShaderPreview, *, reference: bytes, history: PreviewHistory) -> BaseTool:
    """绑定已经固定的参考字节与共享历史, 不读取模型提供的文件路径.

    Args:
        preview: 调用方管理的常驻预览.
        reference: 由工具工厂固定并验证的参考 PNG 字节.
        history: 与原图工具共享的成功截图历史.

    Returns:
        返回并排图、截图编号与当前参数的工具.
    """

    @tool
    def compare_preview() -> list[dict[str, object]]:
        """取当前截图并与参考图原尺度并排, 左参考右当前, 返回截图编号和参数.

        不提供误差、排名或变好/变差结论, 请根据图片自行判断差异.
        """
        try:
            snapshot = preview.capture()
            comparison = side_by_side_png(reference, snapshot.png)
            return image_content(snapshot, trial_id=history.record(snapshot), comparison=comparison)
        except ValueError as exc:
            raise ToolException(str(exc)) from exc

    return configure_tool(compare_preview)
