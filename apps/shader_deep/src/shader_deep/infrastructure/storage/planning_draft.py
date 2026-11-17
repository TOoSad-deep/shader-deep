"""将单份 JSON 草稿绑定到当前观察范围内的 M/S 合同."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.scene_planning import ScenePlanningSubmission, diagnose_scene_planning
from shader_deep.infrastructure.storage.json_draft import JsonDraftStore

if TYPE_CHECKING:
    from _thread import RLock as RLockType
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.domain.scene_observations import SceneObservations

DRAFT_PATH = "/draft/planning.json"


class PlanningDraftStore(JsonDraftStore[ScenePlanningSubmission]):
    """以固定观察为只读诊断上下文, 草稿只维护 M/S."""

    def __init__(self, run_dir: Path, observations: SceneObservations, *, lock: RLockType | None = None) -> None:
        """绑定规划字段与同一份观察的业务诊断.

        Args:
            run_dir: 本次独立规划运行的目录.
            observations: 已捕获的固定全图观察.
            lock: 调用方共用的操作锁.
        """

        def validation(report: ScenePlanningSubmission) -> dict[str, JsonValue]:
            issues = diagnose_scene_planning(report, observations)
            return {"valid": not issues, "issues": [issue.model_dump(mode="json") for issue in issues]}

        super().__init__(run_dir, filename="planning.json", model_type=ScenePlanningSubmission, validation=validation, lock=lock)
