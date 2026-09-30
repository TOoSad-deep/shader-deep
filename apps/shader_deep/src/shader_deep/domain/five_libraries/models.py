"""五库首版字段; 运行身份与发布状态不属于业务对象."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
Selections = dict[str, tuple[Text, ...]]


class FrozenModel(BaseModel):
    """拒绝额外字段, 对象更新生成新版本."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Element(FrozenModel):
    """本图目标元素与观察特征的引用."""

    id: Text
    name: Text
    region: Text
    feature_ids: tuple[Text, ...]


class MechanismRef(FrozenModel):
    """本次观察可采用的机制及适配理由."""

    id: Text
    reason: Text | None = None


class Feature(FrozenModel):
    """带位置的观察及候选机制."""

    id: Text
    description: Text
    mechanism_refs: tuple[MechanismRef, ...]


class Participant(FrozenModel):
    """元素或特征参与关系的作用."""

    ref: Text
    role: Text | None = None


class Relation(FrozenModel):
    """可见联系; 参与方向由作用表达."""

    id: Text
    participants: Annotated[tuple[Participant, ...], Field(min_length=2)]
    description: Text
    mechanism_refs: tuple[MechanismRef, ...]


class Mechanism(FrozenModel):
    """可复用构造方法, 不绑定本图消费者."""

    id: Text
    name: Text
    method: Text
    conditions: tuple[Text, ...] | None = None


class Alternative(FrozenModel):
    """以默认方案为基准的一组共同替换."""

    choices: Selections
    reason: Text
    conditions: tuple[Text, ...] | None = None
    composition: Text | None = None


class Sketch(FrozenModel):
    """既有目标的默认组合、组织说明和局部替代."""

    id: Text
    element_id: Text
    name: Text
    default: Selections
    composition: Text
    alternatives: tuple[Alternative, ...] | None = None


class Issue(FrozenModel):
    """业务未知项或缺口; 空引用表示整轮问题."""

    refs: tuple[Text, ...]
    description: Text


class FiveLibraries(FrozenModel):
    """同一快照的五库对象集合."""

    elements: tuple[Element, ...]
    features: tuple[Feature, ...]
    relations: tuple[Relation, ...]
    mechanisms: tuple[Mechanism, ...]
    sketches: tuple[Sketch, ...]


class ExplorationSubmission(FrozenModel):
    """探索产物提交容器; 不增加业务库字段."""

    features: tuple[Feature, ...]
    relations: tuple[Relation, ...]
    mechanisms: tuple[Mechanism, ...]
    sketches: tuple[Sketch, ...]
    open_questions: tuple[Issue, ...] = ()
    gaps: tuple[Issue, ...] = ()


class MergeGroup(FrozenModel):
    """只保留组内既有项的合并建议."""

    kind: Literal["features", "relations", "mechanisms"]
    ids: Annotated[tuple[Text, ...], Field(min_length=2)]
    keep: Text


class MergeProposal(FrozenModel):
    """一次整体校验、整体接受的合并建议."""

    groups: tuple[MergeGroup, ...] = ()
