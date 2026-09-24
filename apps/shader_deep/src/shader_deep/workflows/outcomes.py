"""工作流对外返回的业务结果."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.tasks import BlackboardState, ResultRecord


@dataclass(frozen=True, kw_only=True)
class AnalysisOutcome:
    """完整或中断的分析结果, 包含运行保留的所有业务记录."""

    state: BlackboardState
    task_id: str
    summary_result: ResultRecord | None
    run_dir: Path
    stop_reason: str
