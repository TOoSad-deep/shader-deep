"""将跨阶段业务历史关联到可由 VISTA 独立回读的图片记录."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.infrastructure.storage.parameterized_candidate import read_candidate_parameters
from shader_deep.infrastructure.storage.vista import publish_history_json, publish_image, read_history_json, read_image

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import JsonValue

    from shader_deep.domain.tasks import CandidateRecord
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate, WorkflowState


def archive_render(root: Path, directory: Path, candidate: CandidateRecord, time: float, *, source_agent: str) -> str:
    """登记实际渲染结果, 复用完全相同的 PNG 和 GLSL 内容."""
    if candidate.preview_path is None:
        msg = "A VISTA image record requires a successful render"
        raise ValueError(msg)
    code, preview = Path(candidate.code_path), Path(candidate.preview_path)
    root = root.resolve()
    for artifact in (code, preview):
        if not artifact.resolve().is_relative_to(root):
            msg = "Rendered artifact is outside the workflow directory"
            raise ValueError(msg)
    values: dict[str, JsonValue] = {}
    if candidate.render_record_path is not None:
        record = Path(candidate.render_record_path)
        if not record.resolve().is_relative_to(root):
            msg = "Parameter record is outside the workflow directory"
            raise ValueError(msg)
        params = read_candidate_parameters(record, code, preview)
        if json.loads(record.read_bytes())["render"]["time"] != time:
            msg = "VISTA record requires the actual candidate time"
            raise ValueError(msg)
        values = {name: list(value) if isinstance(value, tuple) else value for name, value in params.uniforms.items()}
    return publish_image(
        root / "vista",
        label=f"{directory.name}:{candidate.id}",
        png=preview.read_bytes(),
        code=code.read_bytes(),
        source_agent=source_agent,
        render={"time": time, "uniforms": values},
    )


def archive_generation(root: Path, directory: Path, candidates: Iterable[CandidateRecord], time: float) -> None:
    """含停止或未选定的回合也保留该生成目录实际产出的所有可回看候选."""
    for candidate in candidates:
        if candidate.preview_path is not None and Path(candidate.preview_path).resolve().is_relative_to(directory):
            archive_render(root, directory, candidate, time, source_agent="generation")


def verify_render(directory: Path, candidate: WorkflowCandidate, record: CandidateRecord) -> None:
    """核对业务候选与 VISTA 归档的图片、代码和参数是否仍一致."""
    if candidate.vista_record is None:
        return
    png, code, render = read_image(directory / "vista", candidate.vista_record)
    expected = {"time": candidate.time, "uniforms": {}}
    if record.render_record_path is not None:
        expected = json.loads(Path(record.render_record_path).read_bytes())["render"]
    if (
        record.preview_path is None
        or png != Path(record.preview_path).read_bytes()
        or code != Path(record.code_path).read_bytes()
        or render != expected
    ):
        msg = "Candidate differs from its immutable VISTA render"
        raise ValueError(msg)


def _summary(candidate: WorkflowCandidate) -> dict[str, JsonValue]:
    return candidate.model_dump(
        mode="json",
        include={
            "id",
            "vista_record",
            "vista_review_record",
            "vista_diagnosis_record",
            "scope",
            "criteria_sha256",
            "verdict",
            "major_issues",
            "matched_checks",
            "remaining_issues",
            "minor_issue_count",
            "regressions",
            "objective_error",
            "objective_run",
            "review_sha256",
            "code_sha256",
            "time",
        },
    )


def _snapshot(state: WorkflowState) -> dict[str, JsonValue]:
    return {
        "reference": state.vista_reference_record,
        "phase": state.phase,
        "round": state.round,
        "best_candidate": state.best_candidate.id if state.best_candidate is not None else None,
        "manual_best_candidate": state.manual_best_candidate_id,
        "current_candidate": state.current_candidate.id if state.current_candidate is not None else None,
        "candidates": [_summary(item) for item in state.candidates],
        "attempts": [item.model_dump(mode="json") for item in state.attempts],
        **({"attempt_index": state.attempt_index} if state.attempt_index is not None else {}),
        "reason": state.reason if state.phase in {"completed", "blocked"} else None,
    }


def with_history(state: WorkflowState) -> WorkflowState:
    """业务历史先原子发布, 工作流快照最后指向完整版本; 旧快照仍可读取."""
    if state.vista_reference_record is None:
        return state
    root = Path(state.directory)
    current = _snapshot(state)
    if state.vista_history_record is not None:
        previous = read_history_json(root, state.vista_history_record)
        previous.pop("previous", None)
        if previous == current:
            return state
    name = publish_history_json(root, {**current, "previous": state.vista_history_record})
    return state.model_copy(update={"vista_history_record": name})


def read_history(state: WorkflowState) -> dict[str, JsonValue]:
    """恢复当前选择和最佳候选的历史, 拒绝工作流索引和已发布记录不一致."""
    expected = _snapshot(state)
    if state.vista_history_record is None:
        if state.vista_reference_record is not None:
            msg = "Workflow VISTA history record is missing"
            raise ValueError(msg)
        return expected
    current = read_history_json(Path(state.directory), state.vista_history_record)
    current.pop("previous", None)
    if current != expected:
        msg = "Workflow differs from its immutable candidate history"
        raise ValueError(msg)
    return current


def diagnosis_history(state: WorkflowState) -> list[JsonValue]:
    """从固定账本恢复最近尝试和最佳记录供诊断阅读."""
    snapshot = read_history(state)
    candidates = snapshot["candidates"]
    if not isinstance(candidates, list):
        msg = "Invalid candidate list in workflow history"
        raise TypeError(msg)
    history = candidates[-8:]
    attempts = snapshot["attempts"]
    if isinstance(attempts, list) and attempts:
        history.append({"revision_attempts": attempts[-8:]})
    best = next((item for item in candidates if isinstance(item, dict) and item["id"] == snapshot["best_candidate"]), None)
    if best is not None:
        history.append({"best_candidate": best})
    return history
