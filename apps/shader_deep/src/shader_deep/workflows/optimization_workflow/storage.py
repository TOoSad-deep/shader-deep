"""控制面状态、文件身份与跨进程串行化."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.optimization_workflow.contracts import WorkflowState
from shader_deep.workflows.optimization_workflow.history import read_history, with_history

if TYPE_CHECKING:
    from collections.abc import Iterator

WORKFLOW_FILE = "workflow.json"


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(directory: Path, path: Path) -> str:
    return path.resolve().relative_to(directory).as_posix()


def _asset(directory: Path, relative: str) -> Path:
    path = directory / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(directory):
        msg = "Workflow artifact escapes its run directory"
        raise ValueError(msg)
    return path


def _save(state: WorkflowState) -> WorkflowState:
    state = with_history(state)
    write_snapshot(Path(state.directory) / WORKFLOW_FILE, state.model_dump(mode="json"))
    return state


def _trace(state: WorkflowState, event: str, **details: object) -> None:
    """按工作流记录一次决策、阶段边界或产物变更, 不复制输入图像."""
    current = state.current_candidate
    row = {
        "time": datetime.now(UTC).isoformat(),
        "workflow": Path(state.directory).name,
        "revision": state.revision,
        "round": state.round,
        "phase": state.phase,
        "event": event,
        "candidate": current.id if current else None,
        "best": state.best_candidate.id if state.best_candidate else None,
        "observation": state.observation_path,
        "planning": state.planning_path,
        "criteria": state.frozen_criteria_path,
        "review": current.review_path if current else None,
        "diagnosis": current.diagnosis_path if current else None,
        "remaining_model_calls": state.remaining_model_calls,
        "remaining_output_tokens": state.remaining_output_tokens,
        "remaining_total_tokens": state.remaining_total_tokens,
        **details,
    }
    with (Path(state.directory) / "workflow-trace.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_workflow(directory: Path) -> WorkflowState:
    """读取同一独立运行目录的原子状态快照.

    Args:
        directory: 整体流程运行目录.

    Returns:
        可展示的预算、门禁及相对制品索引.

    Raises:
        ValueError: 状态与所在目录身份分离.
    """
    root = Path(directory).resolve()
    state = WorkflowState.model_validate_json((root / WORKFLOW_FILE).read_bytes())
    if Path(state.directory) != root:
        msg = "Workflow state is bound to its original run directory"
        raise ValueError(msg)
    read_history(state)
    return state


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    # 文件描述符关闭时释放进程锁, 进程中断也不会留下无法续办的旧锁.
    import fcntl  # noqa: PLC0415  # 评审发布与控制面使用本地系统文件锁.

    with (directory / ".workflow.lock").open("ab") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = "A workflow transition is already in progress"
            raise ValueError(msg) from exc
        yield
