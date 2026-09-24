"""固定范围比较的提交契约, 不管理业务库."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from shader_deep.domain.library.decisions import MergeDecision
from shader_deep.domain.primitives import Text  # noqa: TC001  # Pydantic 运行时解析文本字段.

ComparisonKind = Literal["feature", "relation", "sketch", "candidate"]


@dataclass(frozen=True)
class ControlledWork:
    """人工指定的局部判断; 依赖只要求已有拥有者完成归并."""

    id: str
    kind: ComparisonKind
    target_ids: tuple[str, ...]
    question: str
    depends_on: str | None = None
    owner_ids: tuple[str, ...] = ()


class FeatureMerge(MergeDecision):
    """受控特征步骤不能请求其他类型的合并."""

    kind: Literal["feature"]


class CandidateMerge(MergeDecision):
    """受控候选步骤不能请求其他类型的合并."""

    kind: Literal["candidate"]


class RelationMerge(MergeDecision):
    """关系比较只允许关系合并."""

    kind: Literal["relation"]


class SketchMerge(MergeDecision):
    """草图比较只允许草图合并."""

    kind: Literal["sketch"]


class FeatureDecision(BaseModel):
    """仅保留现有整合协议在特征比较中允许的字段."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    merges: tuple[FeatureMerge, ...] = ()
    preserve: bool = False
    deferred_work: tuple[Text, ...] = ()


class CandidateDecision(BaseModel):
    """仅保留现有整合协议在候选比较中允许的字段."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    merges: tuple[CandidateMerge, ...] = ()
    preserve: bool = False
    deferred_work: tuple[Text, ...] = ()


class RelationDecision(FeatureDecision):
    """关系比较的受限决定."""

    merges: tuple[RelationMerge, ...] = ()


class SketchDecision(FeatureDecision):
    """组成比较的受限决定."""

    merges: tuple[SketchMerge, ...] = ()
