"""独立探索与可能性库契约; 结构校验不判断机制或视觉效果是否正确."""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from typing import Annotated, Literal

from pydantic import Field
from pydantic.dataclasses import dataclass

from shader_deep.domain.primitives import CONFIG, Text

Texts = Annotated[tuple[Text, ...], Field(min_length=1)]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class InstanceGroup:
    """同一元素中需要明确寻址的实例组."""

    id: Text
    description: Text


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class ElementScope:
    """元素或该元素所属实例组的范围."""

    element_id: Text
    group_id: Text | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class OutlineElement:
    """首次观察统一确定的视觉元素, 不预设实现机制."""

    id: Text
    name: Text
    scope: Text
    salient_features: tuple[Text, ...]
    instance_groups: tuple[InstanceGroup, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class OutlineRelation:
    """只描述可见组织的初稿关系."""

    participants: Annotated[tuple[ElementScope, ...], Field(min_length=2)]
    description: Text


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class VisualOutline:
    """所有独立探索共用的元素身份与可见线索."""

    elements: Annotated[tuple[OutlineElement, ...], Field(min_length=1)]
    relations: tuple[OutlineRelation, ...]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class LibraryElement:
    """最终元素身份及尚未由草图承载的具体问题."""

    id: Text
    name: Text
    scope: Text
    instance_groups: tuple[InstanceGroup, ...] = ()
    unresolved: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Requirement:
    """候选前提: 单项候选至少满足一个, 多项须全部满足."""

    kind: Literal["feature", "relation"]
    id: Text
    candidate_ids: Texts


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Candidate:
    """一种机制及其选择前提, 不代表已选定或已验证."""

    id: Text
    mechanism: Text
    reasoning: Text
    requires: tuple[Requirement, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Participant:
    """关系端点; 实例组只用于元素, 方向只用于依赖关系."""

    kind: Literal["element", "feature", "relation"]
    id: Text
    group_id: Text | None = None
    role: Literal["source", "target"] | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Feature:
    """需要解释的外观及共享局部候选."""

    id: Text
    name: Text
    element_ids: Texts
    appearance: Text
    candidates: Annotated[tuple[Candidate, ...], Field(min_length=1)]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Relation:
    """元素、特征或关系之间的组织与有方向依赖."""

    id: Text
    name: Text
    kind: Literal["organization", "dependency"]
    participants: Annotated[tuple[Participant, ...], Field(min_length=2)]
    description: Text
    candidates: Annotated[tuple[Candidate, ...], Field(min_length=1)]


Scopes = Annotated[tuple[ElementScope, ...], Field(min_length=1)]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class FeatureChoice:
    """子报告保留的特征候选范围, 不表达全局排名."""

    feature_id: Text
    candidate_ids: Texts
    applies_to: Scopes | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class RelationChoice:
    """子报告保留的关系候选范围."""

    relation_id: Text
    candidate_ids: Texts
    applies_to: Scopes | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class CandidateRef:
    """特定草图范围中的尝试优先级; 缺省表示未排序."""

    candidate_id: Text
    priority: Literal["high", "normal", "low"] | None = None
    priority_reason: Text | None = None

    def __post_init__(self) -> None:
        """排序理由须对应明确优先级, 不将未排序解释为低优先级."""
        if self.priority_reason is not None and self.priority is None:
            msg = "priority_reason requires an explicit priority"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class FinalFeatureChoice:
    """最终草图中的特征候选及局部排序."""

    feature_id: Text
    candidate_refs: Annotated[tuple[CandidateRef, ...], Field(min_length=1)]
    applies_to: Scopes | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class FinalRelationChoice:
    """最终草图中的关系候选及局部排序."""

    relation_id: Text
    candidate_refs: Annotated[tuple[CandidateRef, ...], Field(min_length=1)]
    applies_to: Scopes | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class Sketch:
    """整体组成与共享候选引用; 简单组成可仅含一条 composition."""

    id: Text
    name: Text
    element_ids: Texts
    composition: Texts
    feature_refs: tuple[FeatureChoice, ...] = ()
    relation_refs: tuple[RelationChoice, ...] = ()
    unresolved: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class FinalSketch:
    """最终组成草图, 候选引用可附局部优先级."""

    id: Text
    name: Text
    element_ids: Texts
    composition: Texts
    feature_refs: tuple[FinalFeatureChoice, ...] = ()
    relation_refs: tuple[FinalRelationChoice, ...] = ()
    unresolved: tuple[Text, ...] = ()


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class ExplorationReport:
    """独立子任务的三个业务库, 没有运行身份和重复摘要."""

    sketch_library: tuple[Sketch, ...]
    feature_library: tuple[Feature, ...]
    relation_library: tuple[Relation, ...]


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class PossibilityLibrary:
    """最终四库业务对象; 来源和运行版本由后端另行保存."""

    elements: Annotated[tuple[LibraryElement, ...], Field(min_length=1)]
    sketch_library: tuple[FinalSketch, ...]
    feature_library: tuple[Feature, ...]
    relation_library: tuple[Relation, ...]


def compact_data(value: object) -> object:
    """转换为 JSON 数据, 省略无内容的可选字段而保留必填空数组.

    Args:
        value: 契约实例或由契约组成的容器.

    Returns:
        不含无意义可选空值的 JSON 兼容数据.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: compact_data(getattr(value, field.name))
            for field in fields(value)
            if (field.default is MISSING and field.default_factory is MISSING) or getattr(value, field.name) not in (None, (), [], {})
        }
    if isinstance(value, dict):
        return {key: compact_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [compact_data(item) for item in value]
    return value


Element = OutlineElement | LibraryElement
Choice = FeatureChoice | RelationChoice | FinalFeatureChoice | FinalRelationChoice
Library = ExplorationReport | PossibilityLibrary
