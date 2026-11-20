"""视觉评审的固定标准、六维提交与确定性结论."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Pixel = Annotated[int, Field(strict=True, ge=0)]
FiniteTime = Annotated[float, Field(strict=True, allow_inf_nan=False)]
DimensionId = Literal["content", "layout", "shape", "color", "surface", "relations"]
ProfileId = Literal["feature", "element", "scene"]
DimensionStatus = Literal["match", "minor_gap", "major_gap", "uncertain", "not_applicable"]
ReviewVerdict = Literal["needs_revision", "no_major_issue_observed", "inconclusive"]
DIMENSION_IDS: tuple[DimensionId, ...] = ("content", "layout", "shape", "color", "surface", "relations")
PROFILE_VERSION = "1"


class ReviewModel(BaseModel):
    """以不可变嵌套对象和 tuple 保存合同, 拒绝未知字段."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ReviewValidationError(ValueError):
    """携带可定位错误的领域拒绝."""

    def __init__(self, code: str, path: str, message: str) -> None:
        """保存工具可以直接报告的诊断."""
        self.code, self.path, self.message = code, path, message
        super().__init__(f"{path}: {message}")


def _require(condition: bool, code: str, path: str, message: str) -> None:  # noqa: FBT001  # 内部断言辅助的首参数直接表达待校验条件.
    if not condition:
        raise ReviewValidationError(code, path, message)


class PixelRegion(ReviewModel):
    """原始像素坐标中的左闭右开矩形."""

    left: Pixel
    top: Pixel
    right: Pixel
    bottom: Pixel

    @model_validator(mode="after")
    def _nonempty(self) -> Self:
        _require(self.right > self.left and self.bottom > self.top, "empty_region", "/region", "矩形必须具有正宽高")
        return self

    def covers(self, other: PixelRegion) -> bool:
        """检查同一像素坐标系中的矩形是否完整覆盖另一矩形."""
        return self.left <= other.left < other.right <= self.right and self.top <= other.top < other.bottom <= self.bottom


class ReviewSample(ReviewModel):
    """调用方选定的成对统计区域, 均使用各自输入 PNG 的坐标."""

    label: Text
    reference_region: PixelRegion
    rendered_region: PixelRegion


class RequiredRegion(ReviewModel):
    """绑定某项必查内容在共享坐标系中需要看到的完整范围."""

    dimension: DimensionId
    check: Text
    region: PixelRegion


class ReviewCoverage(ReviewModel):
    """调用方给出的 1:1 像素映射, 每个窗口对应输入 PNG 的全部像素."""

    reference_window: PixelRegion
    rendered_window: PixelRegion
    required_regions: Annotated[tuple[RequiredRegion, ...], Field(min_length=1)]


class ReviewConditions(ReviewModel):
    """调用方已知的呈现条件, 未知值保持为空."""

    reference_time: FiniteTime | None = None
    rendered_time: FiniteTime | None = None
    reference_background: Text | None = None
    rendered_background: Text | None = None
    comparison_notes: tuple[Text, ...] = ()


class ReviewSource(ReviewModel):
    """调用方声明的来源, 不构成历史生成过程校验."""

    candidate_id: Text | None = None
    run_id: Text | None = None


class DimensionCriteria(ReviewModel):
    """一维的固定检查内容、排除依据与已有容差."""

    id: DimensionId
    required_checks: tuple[Text, ...] = ()
    excluded_checks: tuple[Text, ...] = ()
    tolerances: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def _explained(self) -> Self:
        _require(bool(self.required_checks or self.excluded_checks), "unexplained_dimension", f"/dimensions/{self.id}", "无必查项时必须说明排除依据")
        return self


def _check_dimensions(identities: tuple[DimensionId, ...], path: str) -> None:
    _require(len(identities) == len(DIMENSION_IDS) and set(identities) == set(DIMENSION_IDS), "invalid_dimensions", path, "六个指标必须各出现一次")


class ReviewRequest(ReviewModel):
    """调用方在评价候选前指定的评审任务."""

    scope: Literal["local", "scene"]
    target_kind: Literal["feature", "element"] | None = None
    target: Text
    requirements: Text
    dimensions: tuple[DimensionCriteria, ...]
    reference_region: PixelRegion | None = None
    rendered_region: PixelRegion | None = None
    conditions: ReviewConditions = Field(default_factory=ReviewConditions)
    source: ReviewSource = Field(default_factory=ReviewSource)
    # 省略未启用的新增字段, 保留旧评审包的冻结标准摘要.
    samples: tuple[ReviewSample, ...] = Field(default=(), exclude_if=lambda value: not value)
    coverage: ReviewCoverage | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _task(self) -> Self:
        _require(
            (self.scope == "local") == (self.target_kind is not None), "invalid_scope", "/target_kind", "局部必须提供特征或元素类型, 整图省略局部类型"
        )
        _check_dimensions(tuple(item.id for item in self.dimensions), "/dimensions")
        _require(any(item.required_checks for item in self.dimensions), "empty_criteria", "/dimensions", "本轮至少包含一项必查内容")
        _require(
            self.scope == "local" or (self.reference_region is None and self.rendered_region is None),
            "scene_region",
            "/scope",
            "整图任务使用完整图像",
        )
        _unique(tuple(sample.label for sample in self.samples), "/samples")
        if self.coverage is not None:
            _validate_coverage_binding(self.coverage, self.dimensions)
        return self


def _validate_coverage_binding(coverage: ReviewCoverage, dimensions: tuple[DimensionCriteria, ...]) -> None:
    checks = {item.id: item.required_checks for item in dimensions}
    keys = set()
    for index, item in enumerate(coverage.required_regions):
        path = f"/coverage/required_regions/{index}"
        key = (item.dimension, item.check)
        _require(item.check in checks[item.dimension], "unknown_check", path, "覆盖区域必须绑定现有的必查项")
        _require(key not in keys, "duplicate_check", path, "同一必查项只能绑定一个区域")
        keys.add(key)


class BoundReviewCriteria(ReviewModel):
    """程序固定的任务、模板版本和完整标准身份."""

    request: ReviewRequest
    profile_id: ProfileId
    profile_version: Literal["1"] = PROFILE_VERSION
    rule_sha256: Digest
    profile_sha256: Digest
    criteria_sha256: Digest


def review_profile(request: ReviewRequest) -> ProfileId:
    """根据合法范围组合确定唯一模板.

    Args:
        request: 本次评审任务.

    Returns:
        特征、元素或整图模板标识.
    """
    if request.scope == "scene":
        return "scene"
    _require(request.target_kind is not None, "invalid_scope", "/target_kind", "局部缺少目标类型")
    return "feature" if request.target_kind == "feature" else "element"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bind_review_criteria(request: ReviewRequest, rule_text: str, profile_text: str) -> BoundReviewCriteria:
    """将本轮任务与实际规则内容固定为不可变标准.

    Args:
        request: 调用方任务及具体验收说明.
        rule_text: 实际共同规则正文.
        profile_text: 实际选定层级模板正文.

    Returns:
        含规则和标准摘要的固定绑定.
    """
    _require(bool(rule_text.strip() and profile_text.strip()), "empty_rules", "/rules", "规则和模板正文必须非空")
    checked = ReviewRequest.model_validate(request.model_dump(mode="json"))
    entries = {item.id: item for item in checked.dimensions}
    checked = checked.model_copy(update={"dimensions": tuple(entries[identity] for identity in DIMENSION_IDS)})
    payload = {
        "request": checked.model_dump(mode="json", exclude={"source"}),
        "profile_id": review_profile(checked),
        "profile_version": PROFILE_VERSION,
        "rule_sha256": _digest(rule_text),
        "profile_sha256": _digest(profile_text),
    }
    identity = _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return BoundReviewCriteria(
        request=checked,
        profile_id=review_profile(checked),
        rule_sha256=_digest(rule_text),
        profile_sha256=_digest(profile_text),
        criteria_sha256=identity,
    )


def validate_bound_criteria(criteria: BoundReviewCriteria, rule_text: str, profile_text: str) -> None:
    """核对固定绑定与其实际规则内容一致.

    Args:
        criteria: 正式读取或执行采用的固定标准.
        rule_text: 共同规则正文.
        profile_text: 所选模板正文.

    Raises:
        ReviewValidationError: 标准内容或模板身份已经改变.
    """
    expected = bind_review_criteria(criteria.request, rule_text, profile_text)
    _require(criteria == expected, "criteria_mismatch", "/criteria", "绑定内容、模板或规则身份不匹配")


class DimensionReview(ReviewModel):
    """一维的视觉判断与具体证据引用."""

    id: DimensionId
    status: DimensionStatus
    rationale: Text
    issue_ids: tuple[Text, ...] = ()
    unassessed_ids: tuple[Text, ...] = ()


class ReviewIssue(ReviewModel):
    """有可见依据的差异, 与算法成因判断分开."""

    id: Text
    dimension: DimensionId
    region: Text
    reference_observation: Text
    rendered_observation: Text
    severity: Literal["minor", "major"]
    impact: Text
    check: Text | None = Field(
        default=None, exclude_if=lambda value: value is None, description="必查项原文。同一维度既有可查项又有程序未评项时必填。"
    )


class UnassessedItem(ReviewModel):
    """范围内必要但证据不足的内容."""

    id: Text
    dimension: DimensionId
    subject: Text
    reason: Text


class RegionStatistics(ReviewModel):
    """从输入图像测出的区域均值及位置."""

    region: PixelRegion
    mean_rgb: tuple[float, float, float]
    mean_luma: float
    pixel_count: int


class PairedRegionStatistics(ReviewModel):
    """同一命名样本在参考与渲染图上的区域统计."""

    label: Text
    reference: RegionStatistics
    rendered: RegionStatistics


class CoverageEvidence(ReviewModel):
    """实际用于局部评审的视图在共享坐标系中的范围及程序未评项."""

    reference_view: PixelRegion
    rendered_view: PixelRegion
    unassessed: tuple[UnassessedItem, ...]


class ReviewEvidence(ReviewModel):
    """固定图片和任务派生出的程序证据."""

    samples: tuple[PairedRegionStatistics, ...] = ()
    units: Literal["encoded_RGB_0_255"] = "encoded_RGB_0_255"
    alpha_background: Literal["white"] = "white"
    coverage: CoverageEvidence | None = None


class ReviewSubmission(ReviewModel):
    """模型提交的逐维判断, 总体结论由程序派生."""

    summary: Text
    dimensions: tuple[DimensionReview, ...]
    issues: tuple[ReviewIssue, ...] = ()
    unassessed: tuple[UnassessedItem, ...] = ()
    limitations: tuple[Text, ...] = ()


def _unique(identities: tuple[str, ...], path: str) -> None:
    _require(len(identities) == len(set(identities)), "duplicate_id", path, "标识或引用不能重复")


def _validate_references(submission: ReviewSubmission) -> None:
    dimensions = {item.id: item for item in submission.dimensions}
    issues = {item.id: item for item in submission.issues}
    unknowns = {item.id: item for item in submission.unassessed}
    for item in submission.dimensions:
        path = f"/dimensions/{item.id}"
        _unique(item.issue_ids, path + "/issue_ids")
        _unique(item.unassessed_ids, path + "/unassessed_ids")
        _require(set(item.issue_ids) <= issues.keys(), "missing_issue", path + "/issue_ids", "引用了不存在的问题")
        _require(set(item.unassessed_ids) <= unknowns.keys(), "missing_unassessed", path + "/unassessed_ids", "引用了不存在的未评项")
        _require(
            all(unknowns[identity].dimension == item.id for identity in item.unassessed_ids), "wrong_dimension", path, "未评项必须由所属指标引用"
        )
    for item in submission.issues:
        _require(item.id in dimensions[item.dimension].issue_ids, "orphan_issue", f"/issues/{item.id}", "问题必须被主要指标引用")
    for item in submission.unassessed:
        _require(item.id in dimensions[item.dimension].unassessed_ids, "orphan_unassessed", f"/unassessed/{item.id}", "未评项必须被所属指标引用")


def _expected_status(item: DimensionReview, criteria: DimensionCriteria, issues: tuple[ReviewIssue, ...]) -> DimensionStatus:
    if not criteria.required_checks:
        return "not_applicable"
    selected = [issue for issue in issues if issue.id in item.issue_ids]
    if selected:
        return "major_gap" if any(issue.severity == "major" for issue in selected) else "minor_gap"
    return "uncertain" if item.unassessed_ids else "match"


def _validate_states(submission: ReviewSubmission, criteria: BoundReviewCriteria) -> None:
    checks = {item.id: item for item in criteria.request.dimensions}
    for item in submission.dimensions:
        path = f"/dimensions/{item.id}"
        if not checks[item.id].required_checks:
            _require(not item.issue_ids and not item.unassessed_ids, "excluded_evidence", path, "范围外指标不能关联问题或必要未评项")
        expected = _expected_status(item, checks[item.id], submission.issues)
        _require(item.status == expected, "inconsistent_status", path + "/status", f"根据绑定与证据, 本项状态应为 {expected}")


def validate_review_submission(submission: ReviewSubmission, criteria: BoundReviewCriteria) -> None:
    """核对六维、证据双向引用与固定范围的状态一致性.

    Args:
        submission: 模型提交的视觉判断.
        criteria: 本轮固定标准.

    Raises:
        ReviewValidationError: 标识、引用、范围或状态不一致.
    """
    _check_dimensions(tuple(item.id for item in submission.dimensions), "/dimensions")
    _unique(tuple(item.id for item in submission.issues), "/issues")
    _unique(tuple(item.id for item in submission.unassessed), "/unassessed")
    _validate_references(submission)
    _validate_states(submission, criteria)


def derive_verdict(submission: ReviewSubmission) -> ReviewVerdict:
    """从已经通过合同校验的报告派生视觉结论.

    Args:
        submission: 已通过 validate_review_submission 的提交.

    Returns:
        需要修改、无法判断或未发现重大差异.
    """
    if any(item.severity == "major" for item in submission.issues):
        return "needs_revision"
    return "inconclusive" if submission.unassessed else "no_major_issue_observed"
