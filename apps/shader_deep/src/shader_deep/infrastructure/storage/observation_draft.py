"""将单份 JSON 草稿绑定到全图观察字段与业务诊断."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.scene_observations import SceneObservationSubmission, diagnose_scene_observations
from shader_deep.infrastructure.storage.json_draft import DraftError, JsonDraftStore

if TYPE_CHECKING:
    from _thread import RLock as RLockType
    from pathlib import Path

    from pydantic import JsonValue

DRAFT_PATH = "/draft/observations.json"
__all__ = ["DRAFT_PATH", "DraftError", "DraftStore"]


def _validation(report: SceneObservationSubmission) -> dict[str, JsonValue]:
    issues = diagnose_scene_observations(report)
    return {"valid": not issues, "issues": [issue.model_dump(mode="json") for issue in issues]}


class DraftStore(JsonDraftStore[SceneObservationSubmission]):
    """保持观察草稿入口, 文件操作由共用存储执行."""

    def __init__(self, run_dir: Path, *, lock: RLockType | None = None) -> None:
        """绑定本轮观察草稿及结构校验.

        Args:
            run_dir: 本次独立运行的目录.
            lock: 调用方共用的操作锁.
        """
        super().__init__(run_dir, filename="observations.json", model_type=SceneObservationSubmission, validation=_validation, lock=lock)
