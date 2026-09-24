"""主 Agent 一次整合决定的业务字段; 包身份和版本由执行器绑定."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from shader_deep.analysis.exploration import ExplorationReport, FinalSketch  # noqa: TC001  # Pydantic 在运行时解析工具字段.
from shader_deep.analysis.schemas import Text  # noqa: TC001  # Pydantic 在运行时解析文本约束.


class MergeDecision(BaseModel):
    """指明明确重复的成员, 后端选择稳定的保留身份."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sketch", "feature", "relation", "candidate"]
    members: Annotated[tuple[Text, ...], Field(min_length=2)]


class OutlineIssueDecision(BaseModel):
    """冻结本轮元素身份后, 明确暂缓或有依据地不采纳初稿问题."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: Text
    disposition: Literal["deferred", "dismissed"]
    reason: Text


class IntegrationDecision(BaseModel):
    """只填写发生变化的内容; preserve 保留比较目标, 与整组暂缓互斥."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    merges: tuple[MergeDecision, ...] = ()
    reference_updates: tuple[FinalSketch, ...] = ()
    additions: ExplorationReport | None = None
    unresolved: dict[str, tuple[Text, ...]] = Field(default_factory=dict)
    outline_issue_decisions: tuple[OutlineIssueDecision, ...] = ()
    review_ids: tuple[Text, ...] = ()
    deferred_work: tuple[Text, ...] = ()
    preserve: bool = False
