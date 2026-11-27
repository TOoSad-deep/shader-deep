"""只回读已记录的截图和参数, 可将固定参考图与历史原尺度并排."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from langchain.tools import tool
from langchain_core.tools import ToolException
from pydantic import StrictInt  # noqa: TC002  # @tool 在运行时解析此注解生成入参 Schema.

from shader_deep.experiments.tools._common import configure_tool, image_content
from shader_deep.imaging.comparison import side_by_side_png

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from shader_deep.experiments._parameter_history import PreviewHistory
    from shader_deep.rendering import PreviewSnapshot


def build_tool(history: PreviewHistory, *, reference: bytes | None = None, history_mode: Literal["original", "paired"] = "original") -> BaseTool:
    """绑定同一次实验的历史; 此工具不需要浏览器依赖.

    Args:
        history: 工厂创建并由截图工具写入的共享历史.
        reference: 工厂固定的参考 PNG 字节, paired 模式必须提供.
        history_mode: original 回读原图; paired 将固定参考与历史图并排.

    Returns:
        只读历史参数和 PNG 的工具.

    Raises:
        ValueError: 模式未知, 或 paired 模式没有固定参考图.
    """
    if history_mode not in {"original", "paired"} or (history_mode == "paired" and reference is None):
        msg = "history_mode must be original or paired; paired requires a fixed reference"
        raise ValueError(msg)

    @tool
    def get_trial_history(trial_ids: list[StrictInt] | None = None) -> list[dict[str, object]]:
        """查看截图历史的完整参数和 PNG, 不评分、不排名、不恢复参数或再次渲染.

        原图模式返回独立历史图; 并排模式左侧为固定参考, 右侧为指定历史图,
        图上的 trial_id 和 revision 对应当时快照, 不代表当前浏览器状态.

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
            try:
                content.extend(_history_content(snapshot, number, reference if history_mode == "paired" else None))
            except ValueError as exc:
                raise ToolException(str(exc)) from exc
        return content

    return configure_tool(get_trial_history)


def _history_content(snapshot: PreviewSnapshot, number: int, reference: bytes | None) -> list[dict[str, object]]:
    if reference is None:
        return image_content(snapshot, trial_id=number)
    comparison = side_by_side_png(reference, snapshot.png, right_label=f"Trial {number} / revision {snapshot.revision}")
    return image_content(snapshot, trial_id=number, comparison=comparison, comparison_layout="reference_left_history_right")
