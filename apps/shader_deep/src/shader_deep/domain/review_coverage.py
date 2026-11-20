"""将程序确认的视图覆盖缺口纳入评审提交与结论."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.visual_review import DimensionReview, ReviewSubmission, _require

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import BoundReviewCriteria, CoverageEvidence, DimensionId, UnassessedItem


def _missing(coverage: CoverageEvidence) -> dict[DimensionId, tuple[UnassessedItem, ...]]:
    return {
        dimension: tuple(item for item in coverage.unassessed if item.dimension == dimension)
        for dimension in dict.fromkeys(item.dimension for item in coverage.unassessed)
    }


def validate_coverage(submission: ReviewSubmission, criteria: BoundReviewCriteria, coverage: CoverageEvidence | None) -> None:
    """确认缺少视图的必查项有程序未评记录, 同维度问题仅引用可查项."""
    if coverage is None or not coverage.unassessed:
        return
    _require(set(coverage.unassessed) <= set(submission.unassessed), "missing_coverage", "/unassessed", "报告缺少程序确认的视图未评项")
    required = {item.id: item.required_checks for item in criteria.request.dimensions}
    issues = {issue.id: issue for issue in submission.issues}
    for item in submission.dimensions:
        missing = {gap.subject for gap in coverage.unassessed if gap.dimension == item.id}
        if not missing:
            continue
        available = set(required[item.id]) - missing
        for identity in item.issue_ids:
            issue = issues[identity]
            _require(
                issue.dimension == item.id and issue.check in available,
                "uncovered_issue",
                f"/issues/{issue.id}/check",
                "该维度含程序未评项。请仅为有覆盖的必查项报告问题, 并在问题的 check 中填写该必查项原文。",
            )


def _add_unknowns(item: DimensionReview, gaps: tuple[UnassessedItem, ...], *, fully_missing: bool) -> DimensionReview:
    notes = " ".join(gap.reason for gap in gaps)
    status = "uncertain" if item.status == "match" else item.status
    return item.model_copy(
        update={
            "status": status,
            "rationale": notes if fully_missing else f"{item.rationale} {notes}",
            "unassessed_ids": (*item.unassessed_ids, *(gap.id for gap in gaps)),
        }
    )


def complete_coverage(submission: ReviewSubmission, criteria: BoundReviewCriteria, coverage: CoverageEvidence | None) -> ReviewSubmission:
    """登记程序未评项, 将视图不足反映在维度状态和报告摘要."""
    if coverage is None or not coverage.unassessed:
        return submission
    missing = _missing(coverage)
    required = {item.id: item.required_checks for item in criteria.request.dimensions}
    dimensions = tuple(
        _add_unknowns(item, missing[item.id], fully_missing=len(missing[item.id]) == len(required[item.id])) if item.id in missing else item
        for item in submission.dimensions
    )
    summary = f"视图覆盖不足, {len(coverage.unassessed)} 个必查项未评。"
    if submission.issues:
        summary += f"已记录 {len(submission.issues)} 个可见差异。"
    return submission.model_copy(
        update={
            "summary": summary,
            "dimensions": dimensions,
            "unassessed": submission.unassessed + coverage.unassessed,
        }
    )


def coverage_only_submission(criteria: BoundReviewCriteria, coverage: CoverageEvidence | None) -> ReviewSubmission | None:
    """所有必查项都缺视图时, 构造仅含程序事实的提交."""
    if coverage is None or len(coverage.unassessed) != sum(len(item.required_checks) for item in criteria.request.dimensions):
        return None
    return ReviewSubmission(
        summary="所有必查项均缺少完整视图。",
        dimensions=tuple(
            DimensionReview(
                id=item.id,
                status="match" if item.required_checks else "not_applicable",
                rationale="视图覆盖不足。" if item.required_checks else " ".join(item.excluded_checks),
            )
            for item in criteria.request.dimensions
        ),
    )
