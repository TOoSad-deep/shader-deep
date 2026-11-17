"""单角色观察工作流的交付合同."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.scene_observations import SceneObservations, SceneObservationSubmission


@dataclass(frozen=True, kw_only=True)
class SceneAnalysisResult:
    """同一接受快照的观察与运行状态, 无提交时没有伪造正文."""

    status: Literal["completed", "partial", "failed"]
    submission: SceneObservationSubmission | None = None
    observations: SceneObservations | None = None
    error: str | None = None
    task_state: dict[str, JsonValue] | None = None
