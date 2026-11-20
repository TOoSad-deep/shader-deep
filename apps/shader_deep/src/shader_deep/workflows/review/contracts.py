"""独立评审的接受结果与公开交付索引."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.domain.review_actions import ReviewActionSummary
    from shader_deep.domain.visual_review import ReviewSubmission

ReviewStatus = Literal["completed", "blocked", "failed"]


@dataclass(frozen=True, kw_only=True)
class ReviewResult:
    """原生图结束后保留的接受事实."""

    status: ReviewStatus
    submission: ReviewSubmission | None = None
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReviewOutcome:
    """一次独立评审的公开交付索引.

    Attributes:
        run_dir: 保存固定输入和执行证据的目录.
        report_dir: 成功发布并正式回读的报告包目录.
        status: 报告交付的运行状态.
        verdict: 已交付报告的视觉结论.
        error: 停止原因或运行诊断.
        execution: 调用、规则与输入身份等运行事实.
        action_summary: 已交付报告的轻微问题数量、重大问题和待补证条目.
    """

    run_dir: Path
    report_dir: Path | None
    status: ReviewStatus
    verdict: str | None = None
    error: str | None = None
    execution: dict[str, JsonValue] = field(default_factory=dict)
    action_summary: ReviewActionSummary | None = None

    @property
    def stop_reason(self) -> ReviewStatus:
        """返回与其他运行入口一致的终态字段."""
        return self.status
