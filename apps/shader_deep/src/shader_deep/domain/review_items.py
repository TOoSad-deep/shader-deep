"""固定 V2 必查项, 将逐项回答和程序未评项汇总成六维报告."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from shader_deep.domain.review_coverage import validate_coverage
from shader_deep.domain.visual_review import (
    DimensionReview,
    EvidenceGap,
    FrozenReviewCheck,
    ReviewIssue,
    ReviewModel,
    ReviewSubmission,
    ReviewValidationError,
    Text,
    UnassessedItem,
    _require,
    dimension_status,
    validate_review_submission,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

    from shader_deep.domain.visual_review import BoundReviewCriteria, CoverageEvidence, DimensionCriteria

RevisionNumber = Annotated[int, Field(strict=True, ge=1)]


class BoundCheck(FrozenReviewCheck):
    """冻结范围内的一项检查, 可携带程序已确认的取证缺口."""

    unassessed: UnassessedItem | None = None


class CheckFinding(ReviewModel):
    """一项检查下的独立差异, 归属和报告引用由程序填写."""

    region: Text
    reference_observation: Text
    rendered_observation: Text
    severity: Literal["minor", "major"] = Field(description="minor 也须指出本项对应部位在参考图与候选图中的具体差别。按该差别对本项要求的影响分级。")
    impact: Text


class CheckAnswer(ReviewModel):
    """模型只提交本检查的观察、可见差异和仍然缺证的原因."""

    check_id: Text = Field(description="必填。逐字复制当前所答检查项的 check_id 字符串。检查项原文、维度名和序号均不能代替此 ID。")
    rationale: Text = Field(description="说明本项在两张图中的可见表现。若两侧相符, 在这里写清相符的依据。")
    findings: tuple[CheckFinding, ...] = Field(
        default=(), description="只登记直接违反当前必查项的可见差异。本项自身未见差异时填 []。其他项的缺陷归入其对应项。"
    )
    unassessed_reason: Text | None = None
    evidence_gap: EvidenceGap | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "附在未评原因旁。现有双图可通过扩窗或追加像素取样取得证据时填 insufficient_crop_or_sample。需要补测具体位置时提供共享画布坐标。"
            "缺少可比时间帧或背景上下文时填 missing_context_or_frame。"
        ),
    )


class CheckRevision(ReviewModel):
    """记录一次显式重评的前后答案、理由、可复核依据和实际模型轮次."""

    before: CheckAnswer
    after: CheckAnswer
    revision: RevisionNumber
    model_call: RevisionNumber
    reason: Text
    evidence: Text

    @classmethod
    def from_answer(
        cls, before: CheckAnswer | None, after: CheckAnswer, *, current: int, expected: int, reason: str, evidence: str, model_call: int
    ) -> CheckRevision:
        """检查当前版本及原答案, 创建下一版. 首次登记的版本为 1."""
        path = f"/answers/{after.check_id}"
        if before is None:
            code, msg = "unrecorded_check", f"检查项 {after.check_id} 尚未登记, 请先登记原始答案"
            raise ReviewValidationError(code, path, msg)
        _require(type(expected) is int and expected == current, "stale_revision", path, f"答案当前为第 {current} 版, 请按当前版本复核")
        _require(before.check_id == after.check_id and before != after, "unchanged_revision", path, "重评须改变同一检查项的答案")
        return cls(before=before, after=after, revision=current + 1, model_call=model_call, reason=reason, evidence=evidence)


def validate_check_revisions(
    criteria: BoundReviewCriteria, coverage: CoverageEvidence | None, answers: tuple[CheckAnswer, ...] | None, metadata: Mapping[str, JsonValue]
) -> None:
    """从固定检查范围、逐项审计和交付答案检验重评链.

    Args:
        criteria: 固定的版本和检查标准.
        coverage: 同次采集得到的程序未评项.
        answers: 正式报告的最终逐项答案.
        metadata: 含可选重评审计及实际模型调用数的运行记录.

    Raises:
        ValueError: 协议、记录格式、版本顺序、模型轮次或最终答案不一致.
    """
    if "check_revisions" not in metadata:
        return
    path = "/metadata/check_revisions"
    raw, calls = metadata["check_revisions"], metadata.get("model_calls")
    _require(criteria.request.protocol == "visual_review_v2" and answers is not None, "invalid_revision_protocol", path, "逐项重评需要 V2 逐项答案")
    if not isinstance(raw, list) or not raw or type(calls) is not int or calls < 1:
        msg = f"{path}: 审计必须为非空列表并具有有效的模型调用数"
        raise ValueError(msg)
    budget = metadata.get("max_model_calls")
    _require(budget is None or (type(budget) is int and calls <= budget), "invalid_revision_budget", path, "实际重评轮次不能超过本轮逻辑预算")
    check_index = {check.check_id: check for check in bound_checks(criteria, coverage)}
    latest: dict[str, CheckRevision] = {}
    last_call = 0
    for index, value in enumerate(raw):
        revision = CheckRevision.model_validate(value)
        check_id = revision.after.check_id
        _validate_answer_id(revision.after, check_index)
        _validate_revision_step(revision, latest.get(check_id), f"{path}/{index}")
        _require(last_call <= revision.model_call <= calls, "invalid_revision_round", path, "重评轮次须按序且处于本次实际模型调用范围内")
        latest[check_id], last_call = revision, revision.model_call
    final = {answer.check_id: answer for answer in answers or ()}
    _require(all(final.get(key) == item.after for key, item in latest.items()), "revision_answer_mismatch", path, "审计末次答案与正式逐项答案不符")


def _validate_revision_step(revision: CheckRevision, previous: CheckRevision | None, path: str) -> None:
    _require(revision.before.check_id == revision.after.check_id, "revision_check_mismatch", path, "重评前后必须属于同一检查项")
    _require(revision.before != revision.after, "unchanged_revision", path, "重评须提供发生变化的检查答案")
    expected = previous.revision + 1 if previous is not None else 2
    _require(revision.revision == expected, "revision_out_of_order", path, f"此处应为第 {expected} 版答案")
    _require(previous is None or previous.after == revision.before, "broken_revision_chain", path, "前次重评结果与本次重评起点不一致")


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
        BoundCheck(
            check_id=item.check_id,
            dimension=item.dimension,
            check=item.check,
            source_ref=item.source_ref,
            unassessed=gaps.get((item.dimension, item.check)),
        )
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
    _require(answer.evidence_gap is None or answer.unassessed_reason is not None, "missing_reason", path, "缺证分类须附未评原因")
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
                ReviewIssue(
                    id=f"{check.check_id}-issue-{index}",
                    dimension=check.dimension,
                    check=check.check,
                    source_ref=check.source_ref,
                    **finding.model_dump(),
                )
            )
        if answer.unassessed_reason is not None:
            self.unassessed.append(
                UnassessedItem(
                    id=f"{check.check_id}-unassessed",
                    dimension=check.dimension,
                    subject=check.check,
                    reason=answer.unassessed_reason,
                    evidence_gap=answer.evidence_gap,
                )
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
