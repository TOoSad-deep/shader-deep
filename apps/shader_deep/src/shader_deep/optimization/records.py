"""按已选择的候选恢复真实调参目标、关键转折点和对应生效值."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field

from shader_deep.domain.shader_parameters import UniformValue  # noqa: TC001  # 校验生成阶段的优化记录.
from shader_deep.domain.visual_review import PixelRegion, Text  # noqa: TC001  # Pydantic 解析运行时记录.
from shader_deep.optimization.objective import ProtectedRegion  # noqa: TC001  # Pydantic 需解析保护区.
from shader_deep.optimization.uniforms import OptimizationStatus  # noqa: TC001  # Pydantic 运行时校验优化状态.

if TYPE_CHECKING:
    from pathlib import Path

_Error = Annotated[float, Field(ge=0, le=1)]


class _GenerationOptimization(BaseModel):
    status: OptimizationStatus
    baseline_candidate_id: Text
    candidate_id: Text | None = None
    start_error: _Error
    end_error: _Error
    best_observed_error: _Error
    regions: tuple[PixelRegion, ...] = ()
    anchors: tuple[PixelRegion, ...] = ()
    protected_regions: tuple[ProtectedRegion, ...] = ()
    uniforms: dict[str, UniformValue]
    boundary_parameters: tuple[Text, ...] = ()
    evaluations: Annotated[int, Field(strict=True, ge=1)]
    rejected_trials: Annotated[int, Field(strict=True, ge=0)]
    turning_points: tuple[_TurningPointRecord, ...] = ()

    @property
    def evidence_regions(self) -> tuple[PixelRegion, ...]:
        return tuple(dict.fromkeys((*self.regions, *self.anchors, *(item.region for item in self.protected_regions))))


class _TurningPointRecord(BaseModel):
    candidate_id: Text
    evaluation: Annotated[int, Field(strict=True, ge=1)]
    error: _Error
    uniforms: dict[str, UniformValue]


def selected_optimization(directory: Path, candidate_id: str) -> _GenerationOptimization | None:
    """从本轮记录中找出真正被选择的原基线、赢家或关键转折点."""
    for path in sorted(directory.glob("optimization-*.json"), reverse=True):
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict):
            continue
        if (data.get("status") == "improved" and data.get("candidate_id") == candidate_id) or (
            data.get("status") in ("baseline_satisfied", "structure_bottleneck") and data.get("baseline_candidate_id") == candidate_id
        ):
            return _GenerationOptimization.model_validate(data)
        if data.get("status") == "improved":
            result = _GenerationOptimization.model_validate(data)
            for point in result.turning_points:
                if point.candidate_id == candidate_id:
                    return result.model_copy(update={"candidate_id": candidate_id, "end_error": point.error, "uniforms": point.uniforms})
    return None
