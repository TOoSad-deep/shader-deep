"""探索子任务输入输出及初稿问题反馈."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shader_deep.domain.library.models import ExplorationReport
    from shader_deep.runtime.execution import AnalysisExecution


@dataclass(frozen=True, kw_only=True)
class OutlineIssue:
    """子任务反馈初稿问题, 不自行改变统一元素身份."""

    region: str
    description: str
    element_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ExplorationOutcome:
    """三库与后端运行记录分开, 失败不伪装成空的成功报告."""

    report: ExplorationReport | None
    execution: AnalysisExecution
    issues: tuple[OutlineIssue, ...]
    error: str | None
