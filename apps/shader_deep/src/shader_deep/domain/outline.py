"""初稿问题只允许观察文字修订, 身份和引用范围保持固定."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field

from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.library.validation import validate_outline
from shader_deep.domain.primitives import Text  # noqa: TC001  # Pydantic 在运行时解析工具字段.

if TYPE_CHECKING:
    from shader_deep.domain.library.models import VisualOutline


class IssueProposal(BaseModel):
    """每项问题明确提出保留、驳回或文本修订, 关闭还需下一请求复核."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    issue_id: Text
    disposition: Literal["dismissed", "deferred", "revised"]
    reason: Text


class OutlineReview(BaseModel):
    """受限文本补丁, 不接收元素、实例或关系端点替换."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decisions: tuple[IssueProposal, ...]
    element_features: dict[str, tuple[Text, ...]] = Field(default_factory=dict)
    relation_descriptions: dict[str, Text] = Field(default_factory=dict)


class IssueVerification(BaseModel):
    """对已呈现的提案逐项复核; 不确认时保留未完成问题."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    issue_id: Text
    confirmed: bool
    reason: Text


class VerifyOutlineReview(BaseModel):
    """复核阶段只能确认或保留问题, 不同时修改观察文字."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decisions: tuple[IssueVerification, ...]


def review_error(path: str, message: str) -> NoReturn:
    """将受限修订错误定位到当前提交字段."""
    raise AnalysisValidationError([{"path": path, "code": "invalid_outline_review", "message": message}])


def require_issue_ids(actual: list[str], expected: set[str]) -> None:
    """禁止遗漏、重复或伪造问题身份."""
    if len(actual) != len(set(actual)) or set(actual) != expected:
        review_error("/decisions", "Decide each presented issue exactly once")


def revised_outline(outline: VisualOutline, issues: list[dict[str, object]], review: OutlineReview) -> VisualOutline:
    """校验修订针对反馈中的已有对象, 不修改原始探索任务及报告.

    Args:
        outline: 当前完整初稿.
        issues: 本轮实际呈现的初稿问题.
        review: 模型提出的逐项处置和文字补丁.

    Returns:
        身份与端点不变的新初稿.
    """
    known = {str(issue["id"]): set(cast("list[str]", issue.get("element_ids", []))) for issue in issues}
    require_issue_ids([item.issue_id for item in review.decisions], set(known))
    revised = [item.issue_id for item in review.decisions if item.disposition == "revised"]
    allowed = set().union(*(known[identity] for identity in revised))
    if set(review.element_features) - allowed:
        review_error("/element_features", "Only existing elements of revised issues may change")
    changed: set[str] = set()
    elements = []
    for element in outline.elements:
        features = review.element_features.get(element.id, element.salient_features)
        if element.id in review.element_features and not features:
            review_error("/element_features", "Retain concrete observations; do not erase all features")
        if features != element.salient_features:
            changed.add(element.id)
        elements.append(replace(element, salient_features=features))
    relations = list(outline.relations)
    for key, description in review.relation_descriptions.items():
        if not key.isascii() or not key.isdigit() or str(int(key)) != key or int(key) >= len(relations):
            review_error("/relation_descriptions", "Use an existing relation index")
        relation = relations[int(key)]
        endpoints = {item.element_id for item in relation.participants}
        if not endpoints.intersection(allowed):
            review_error("/relation_descriptions/" + key, "Relation is outside revised issue scope")
        if description != relation.description:
            changed.update(endpoints)
        relations[int(key)] = replace(relation, description=description)
    if any(not known[identity].intersection(changed) for identity in revised) or (changed and not revised):
        review_error("/decisions", "A revised issue requires a concrete related text change")
    result = replace(outline, elements=tuple(elements), relations=tuple(relations))
    validate_outline(result)
    return result
