"""只负责将模型提交的参数值交给已有预览会话校验并更新."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.tools import ToolException
from pydantic import StrictFloat  # noqa: TC002  # @tool 在运行时解析此注解生成入参 Schema.

from shader_deep.experiments.tools._common import configure_tool
from shader_deep.rendering import RenderError

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.rendering import ShaderPreview


def build_tool(preview: ShaderPreview) -> BaseTool:
    """绑定调参工具; 允许范围和当前状态仍由 preview 管理.

    Args:
        preview: 调用方管理的常驻预览.

    Returns:
        只更新允许参数值的工具.
    """

    @tool
    def set_parameters(parameters: dict[str, StrictFloat]) -> str:
        """更新允许的 uniform 参数, 未提供的参数保留当前值; 更新后需单独取图.

        Args:
            parameters: 允许参数的新数值, 必须位于配置范围内.
        """
        try:
            return json.dumps({"parameters": preview.set_parameters(parameters)}, ensure_ascii=False)
        except RenderError as exc:
            # 只有参数范围/名称错误可由模型改值修正; 浏览器等故障直接终止图.
            if exc.stage != "uniform":
                raise
            raise ToolException(str(exc)) from exc
        except ValueError as exc:
            raise ToolException(str(exc)) from exc

    # 说明帮助模型选值, 真正的范围校验仍由预览执行.
    set_parameters.description += "\n允许的参数配置: " + json.dumps(preview.parameter_specs, ensure_ascii=False)

    return configure_tool(set_parameters)
