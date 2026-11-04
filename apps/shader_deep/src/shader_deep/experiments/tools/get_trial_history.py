"""只回读已记录的原始截图和参数, 不操作预览或评价结果."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.tools import ToolException
from pydantic import StrictInt  # noqa: TC002  # @tool 在运行时解析此注解生成入参 Schema.

from shader_deep.experiments.tools._common import configure_tool, image_content

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.experiments._parameter_history import PreviewHistory


def build_tool(history: PreviewHistory) -> BaseTool:
    """绑定同一次实验的历史; 此工具不需要浏览器依赖.

    Args:
        history: 工厂创建并由截图工具写入的共享历史.

    Returns:
        只读历史参数和 PNG 的工具.
    """

    @tool
    def get_trial_history(trial_ids: list[StrictInt] | None = None) -> list[dict[str, object]]:
        """查看截图历史的参数和原始 PNG, 不评分、不排名、不恢复参数或再次渲染.

        Args:
            trial_ids: 指定至多三个截图编号; 不传时返回最近三张, 按记录顺序展示.
                编号对应成功的 capture_preview 或 compare_preview, 不是参数更新次数.
        """
        try:
            count, records = history.read(trial_ids)
        except ValueError as exc:
            raise ToolException(str(exc)) from exc
        summary = {"total_trials": count, "trial_ids": [number for number, _ in records]}
        content: list[dict[str, object]] = [{"type": "text", "text": json.dumps(summary)}]
        for number, snapshot in records:
            content.extend(image_content(snapshot, trial_id=number))
        return content

    return configure_tool(get_trial_history)
