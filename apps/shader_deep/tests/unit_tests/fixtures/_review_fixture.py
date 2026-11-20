"""领域、存储与原生图行为测试共用的最小评审材料."""

from __future__ import annotations

from shader_deep.domain.visual_review import (
    DIMENSION_IDS,
    BoundReviewCriteria,
    DimensionCriteria,
    DimensionId,
    DimensionReview,
    ReviewRequest,
    ReviewSubmission,
    bind_review_criteria,
)


def review_request(*, required: tuple[DimensionId, ...] = ("shape",)) -> ReviewRequest:
    """构造只检查明确维度的整图任务."""
    return ReviewRequest(
        scope="scene",
        target="画面中的圆形",
        requirements="比较圆形的可见轮廓",
        dimensions=tuple(
            DimensionCriteria(id=identity, required_checks=("符合参考表现",))
            if identity in required
            else DimensionCriteria(id=identity, excluded_checks=("本次只检查指定表现",))
            for identity in DIMENSION_IDS
        ),
    )


def review_criteria(request: ReviewRequest | None = None) -> BoundReviewCriteria:
    """构造带独立规则身份的固定标准."""
    return bind_review_criteria(request or review_request(), "共同规则", "整图模板")


def matched_review(criteria: BoundReviewCriteria | None = None) -> ReviewSubmission:
    """构造范围内全部匹配的合法报告."""
    selected = criteria or review_criteria()
    return ReviewSubmission(
        summary="本轮范围内未发现可见差异",
        dimensions=tuple(
            DimensionReview(id=item.id, status="match" if item.required_checks else "not_applicable", rationale="按本次固定范围核对")
            for item in selected.request.dimensions
        ),
    )
