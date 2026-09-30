"""主角色只登记元素范围与独立探索方向."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictBool, model_validator

from shader_deep.domain.five_libraries.models import Element, FrozenModel, Text


class TargetPlan(FrozenModel):
    """一次冻结的目标及单批任务, 不含共享特征观察."""

    elements: Annotated[tuple[Element, ...], Field(min_length=1)]
    target_element_id: Text
    directions: Annotated[tuple[Text, ...], Field(min_length=2, max_length=3)]

    @model_validator(mode="after")
    def validate_scope(self) -> TargetPlan:
        """拒绝重复身份、预填观察与同义重复方向."""
        identifiers = [item.id for item in self.elements]
        if len(set(identifiers)) != len(identifiers) or self.target_element_id not in identifiers:
            msg = "目标必须引用一个唯一已登记元素"
            raise ValueError(msg)
        if any(item.feature_ids for item in self.elements):
            msg = "主角色只能定位元素, feature_ids 必须为空"
            raise ValueError(msg)
        if any(not item.strip() for item in self.directions) or len({item.strip() for item in self.directions}) != len(self.directions):
            msg = "探索方向必须非空且互不重复"
            raise ValueError(msg)
        return self

    @property
    def target(self) -> Element:
        """返回已冻结的唯一分析目标."""
        return next(item for item in self.elements if item.id == self.target_element_id)


class RecoveryDecision(FrozenModel):
    """主 agent 只决定固定任务的最后一次重派, 不重新设计分析."""

    retry: StrictBool
    reason: Text

    @model_validator(mode="after")
    def validate_reason(self) -> RecoveryDecision:
        """最后重派或结束都保留具体诊断理由."""
        if not self.reason.strip():
            msg = "恢复决定必须说明具体理由"
            raise ValueError(msg)
        return self
