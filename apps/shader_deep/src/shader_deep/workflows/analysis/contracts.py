"""单角色观察工作流的交付合同."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.domain.scene_observations import ReferenceMeasurement, SceneObservations, SceneObservationSubmission
    from shader_deep.domain.scene_planning import ObservationRevisionRequest


@dataclass(frozen=True, kw_only=True)
class AnalysisRevision:
    """从同一参考图的旧观察包定向复核, 每次生成新包."""

    observation_dir: Path
    request: ObservationRevisionRequest


@dataclass(frozen=True, kw_only=True)
class SceneAnalysisResult:
    """同一接受快照的观察与运行状态, 无提交时没有伪造正文."""

    status: Literal["completed", "partial", "failed"]
    submission: SceneObservationSubmission | None = None
    observations: SceneObservations | None = None
    error: str | None = None
    task_state: dict[str, JsonValue] | None = None
    reference_measurements: tuple[ReferenceMeasurement, ...] = ()
