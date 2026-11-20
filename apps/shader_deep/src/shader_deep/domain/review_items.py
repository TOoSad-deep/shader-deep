"""固定 V2 必查项, 将逐项回答和程序未评项汇总成六维报告."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from shader_deep.domain.review_coverage import validate_coverage
from shader_deep.domain.visual_review import (
    DimensionReview,
    FrozenReviewCheck,
    ReviewIssue,
    ReviewModel,
    ReviewSubmission,
    Text,
    UnassessedItem,
    _require,
    dimension_status,
    validate_review_submission,
)

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import BoundReviewCriteria, CoverageEvidence, DimensionCriteria


class BoundCheck(FrozenReviewCheck):
    """冻结范围内的一项检查, 可携带程序已确认的取证缺口."""

    unassessed: UnassessedItem | None = None


class CheckFinding(ReviewModel):
    """一项检查下的独立差异, 归属和报告引用由程序填写."""

    region: Text
    reference_observation: Text
    rendered_observation: Text
    severity: Literal["minor", "major"]
    impact: Text


class CheckAnswer(ReviewModel):
    """模型只提交本检查的观察、可见差异和仍然缺证的原因."""

    check_id: Text
    rationale: Text
    findings: tuple[CheckFinding, ...] = ()
    unassessed_reason: Text | None = None


def bound_checks(criteria: BoundReviewCriteria, coverage: CoverageEvidence | None) -> tuple[BoundCheck, ...]:
    """在冻结的必查项映射上关联程序确认的覆盖缺口.

    Args:
        criteria: 评价前冻结的 V2 标准.
        coverage: 固定视图的程序覆盖证据.

    Returns:
        按冻结清单排列的全部必查项, 包含锁定项.
    """
    _require(criteria.request.protocol == "visual_review_v2", "invalid_protocol", "/protocol", "逐项提交需要 V2 评审绑定")
    gaps = {(gap.dimension, gap.subject): gap for gap in coverage.unassessed} if coverage else {}
    return tuple(
        BoundCheck(check_id=item.check_id, dimension=item.dimension, check=item.check, unassessed=gaps.get((item.dimension, item.check)))
        for item in criteria.checks
    )


def validate_check_answer(answer: CheckAnswer, checks: tuple[BoundCheck, ...]) -> None:
    """拒绝不属于本绑定或已被程序锁定的回答.

    Args:
        answer: 模型要提交的单项观察.
        checks: 程序从本次固定输入派生的清单.

    Raises:
        ReviewValidationError: 检查项不存在, 或已有程序确认的缺口.
    """
    _validate_answer_id(answer, {check.check_id: check for check in checks})


def _validate_answer_id(answer: CheckAnswer, by_id: dict[str, BoundCheck]) -> None:
    path = f"/answers/{answer.check_id}"
    _require(answer.check_id in by_id, "unknown_check", path, f"检查项 {answer.check_id} 不在本轮必查范围内")
    _require(by_id[answer.check_id].unassessed is None, "locked_check", path, f"检查项 {answer.check_id} 已由程序登记未评")


def _index_answers(checks: tuple[BoundCheck, ...], answers: tuple[CheckAnswer, ...]) -> dict[str, CheckAnswer]:
    by_id: dict[str, CheckAnswer] = {}
    check_index = {check.check_id: check for check in checks}
    for answer in answers:
        _validate_answer_id(answer, check_index)
        _require(answer.check_id not in by_id, "duplicate_answer", f"/answers/{answer.check_id}", f"重复回答检查项 {answer.check_id}")
        by_id[answer.check_id] = answer
    missing = [check.check_id for check in checks if check.unassessed is None and check.check_id not in by_id]
    _require(not missing, "missing_answers", "/answers", f"尚未回答的必查项: {', '.join(missing)}")
    return by_id


@dataclass
class _DimensionEvidence:
    notes: list[str] = field(default_factory=list)
    issues: list[ReviewIssue] = field(default_factory=list)
    unassessed: list[UnassessedItem] = field(default_factory=list)

    def add_answer(self, check: BoundCheck, answer: CheckAnswer) -> None:
        self.notes.append(f"{check.check}: {answer.rationale}")
        for index, finding in enumerate(answer.findings, start=1):
            self.issues.append(
                ReviewIssue(id=f"{check.check_id}-issue-{index}", dimension=check.dimension, check=check.check, **finding.model_dump())
            )
        if answer.unassessed_reason is not None:
            self.unassessed.append(
                UnassessedItem(id=f"{check.check_id}-unassessed", dimension=check.dimension, subject=check.check, reason=answer.unassessed_reason)
            )

    def review(self, criteria: DimensionCriteria) -> DimensionReview:
        unknowns = tuple(gap.id for gap in self.unassessed)
        return DimensionReview(
            id=criteria.id,
            status=dimension_status(criteria, tuple(self.issues), unknowns),
            rationale="\n".join(self.notes or criteria.excluded_checks),
            issue_ids=tuple(issue.id for issue in self.issues),
            unassessed_ids=unknowns,
        )


def summarize_checks(
    criteria: BoundReviewCriteria,
    coverage: CoverageEvidence | None,
    answers: tuple[CheckAnswer, ...],
    *,
    summary: str,
    limitations: tuple[str, ...] = (),
) -> ReviewSubmission:
    """将每个已回答或由程序锁定的检查项汇总为可正式回读的报告.

    Args:
        criteria: 本轮固定的 V2 标准.
        coverage: 程序从固定输入计算的视图覆盖.
        answers: 待评项的全部回答, 允许按任意顺序提交.
        summary: 整体观察摘要.
        limitations: 不影响本轮必查项的一般限制.

    Returns:
        已校验六维状态、问题归属和程序未评项的报告.

    Raises:
        ReviewValidationError: 存在重复、遗漏、越界或覆盖锁定项的回答.
    """
    checks = bound_checks(criteria, coverage)
    by_id = _index_answers(checks, answers)
    evidence = {item.id: _DimensionEvidence() for item in criteria.request.dimensions}
    for check in checks:
        dimension = evidence[check.dimension]
        if check.unassessed is None:
            dimension.add_answer(check, by_id[check.check_id])
        else:
            dimension.notes.append(f"{check.check}: {check.unassessed.reason}")
            dimension.unassessed.append(check.unassessed)
    report = ReviewSubmission(
        summary=summary,
        dimensions=tuple(evidence[item.id].review(item) for item in criteria.request.dimensions),
        issues=tuple(issue for dimension in evidence.values() for issue in dimension.issues),
        unassessed=tuple(gap for dimension in evidence.values() for gap in dimension.unassessed),
        limitations=limitations,
    )
    validate_review_submission(report, criteria)
    validate_coverage(report, criteria, coverage)
    return report
