"""规划工作流的冻结结果与公开文件索引."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.domain.scene_planning import ObservationRevisionRequest, ScenePlanningSubmission

PlanningStatus = Literal["completed", "partial", "failed"]


@dataclass(frozen=True, kw_only=True)
class ScenePlanningResult:
    """原生图结束后的接受事实, 正式发布由入口执行."""

    status: PlanningStatus
    submission: ScenePlanningSubmission | None = None
    error: str | None = None
    execution: dict[str, JsonValue] = field(default_factory=dict)
    observation_revision_request: ObservationRevisionRequest | None = None


@dataclass(frozen=True, kw_only=True)
class PlanningOutcome:
    """一次完整规划运行的公开索引.

    Attributes:
        run_dir: 独立运行目录.
        report_dir: 已成功发布的正式包目录.
        status: 实际完成、部分完成或失败状态.
        error: 受阻、模型或发布错误.
        execution: 调用计数、固定输入及接受版本等运行事实.
        observation_revision_request: 规划受阻时的可选定向复核申请.
    """

    run_dir: Path
    report_dir: Path | None
    status: PlanningStatus
    error: str | None = None
    execution: dict[str, JsonValue] = field(default_factory=dict)
    observation_revision_request: ObservationRevisionRequest | None = None

    @property
    def stop_reason(self) -> PlanningStatus:
        """返回与其他工作流一致的终态索引名称."""
        return self.status
