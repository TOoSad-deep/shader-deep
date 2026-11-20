"""从正式评审条目派生供下游使用的修订与补证摘要."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self

from pydantic import Field, model_validator

from shader_deep.domain.visual_review import ReviewIssue, ReviewModel, UnassessedItem

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import ReviewSubmission


class ReviewActionSummary(ReviewModel):
    """保留原条目位置与观察依据, 小问题按唯一 ID 计数."""

    minor_issue_count: Annotated[int, Field(strict=True, ge=0)]
    has_minor_issues: bool
    major_issues: tuple[ReviewIssue, ...]
    unassessed: tuple[UnassessedItem, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.has_minor_issues != (self.minor_issue_count > 0):
            msg = "轻微问题标记必须与数量一致"
            raise ValueError(msg)
        return self


def summarize_review_actions(submission: ReviewSubmission) -> ReviewActionSummary:
    """按正式问题清单汇总, 同一问题被多个维度引用时只计一次.

    Args:
        submission: 已通过评审合同校验的逐维报告.

    Returns:
        轻微问题数量、需要修正的重大问题和仍待补证的条目.
    """
    minor_ids = {issue.id for issue in submission.issues if issue.severity == "minor"}
    return ReviewActionSummary(
        minor_issue_count=len(minor_ids),
        has_minor_issues=bool(minor_ids),
        major_issues=tuple(issue for issue in submission.issues if issue.severity == "major"),
        unassessed=submission.unassessed,
    )
