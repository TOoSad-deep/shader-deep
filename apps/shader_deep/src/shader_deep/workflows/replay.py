"""从冻结探索输入重新执行有界主整合, 不恢复旧整合决定或模型历史."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

from pydantic import TypeAdapter

from shader_deep.domain.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.domain.legacy import AnalysisSummary, LensConfig, LensReport
from shader_deep.domain.library.models import ExplorationReport, PossibilityLibrary, VisualOutline
from shader_deep.domain.library.validation import validate_outline
from shader_deep.domain.tasks import ResultRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.workflows.configuration import resolve_phase_options
from shader_deep.workflows.coordinator import ManagedIntegrationSession
from shader_deep.workflows.options import MIN_ANALYSIS_TASKS, AnalysisOptions
from shader_deep.workflows.state import PROTOCOL

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.workflows.outcomes import AnalysisOutcome


class ReplaySession(Protocol):
    """当前与历史实验会话共用的冻结输入装配边界."""

    state: BlackboardState
    task_id: str
    options: AnalysisOptions
    directory: Path
    outline: VisualOutline | None
    outline_versions: list[VisualOutline]
    issues: list[dict[str, object]]
    workers: dict[str, AnalysisExecution]
    directions: list[str]
    store: LibraryStore | None

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """绑定新运行的输入与目录."""
        ...

    def _require_store(self) -> LibraryStore: ...

    def save(self) -> None:
        """保存尚未执行整合的冻结输入."""
        ...


_Session = TypeVar("_Session", bound=ReplaySession)


@dataclass(frozen=True, kw_only=True)
class ReplayInput:
    """完整验证后的冻结输入; 不包含上次运行的整合状态."""

    state: BlackboardState
    task_id: str
    outline: VisualOutline
    issues: list[dict[str, object]]
    workers: dict[str, AnalysisExecution]
    reference: bytes
    source_hash: str
    reference_hash: str


def _mapping(value: object) -> dict[str, object]:
    """只接受 JSON 对象, 不将不完整快照猜补为成功记录."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = "Replay snapshot requires a JSON object"
        raise ValueError(msg)
    return cast("dict[str, object]", value)


def _task(value: object) -> TaskRecord:
    adapter = TypeAdapter(TaskRecord)
    adapter.rebuild(_types_namespace={"LensConfig": LensConfig, "VisualOutline": VisualOutline})
    return adapter.validate_python(value)


def _result(value: object) -> ResultRecord:
    adapter = TypeAdapter(ResultRecord)
    adapter.rebuild(
        _types_namespace={
            "LensReport": LensReport,
            "AnalysisSummary": AnalysisSummary,
            "ExplorationReport": ExplorationReport,
            "PossibilityLibrary": PossibilityLibrary,
        }
    )
    return adapter.validate_python(value)


def _root_state(board: dict[str, object], task_id: str) -> BlackboardState:
    root = _task(_mapping(board["tasks"])[task_id])
    if root.role != "analysis" or root.parent_task_id is not None or root.lens_config is not None:
        msg = "Replay requires a root analysis task"
        raise ValueError(msg)
    target = TypeAdapter(TargetRecord).validate_python(_mapping(board["targets"])[root.target_version])
    return add_task(add_target(new_blackboard(), target), root)


def _restore_tasks(state: BlackboardState, board: dict[str, object], task_id: str, outline: VisualOutline) -> BlackboardState:
    for value in _mapping(board["tasks"]).values():
        data = _mapping(value)
        if data.get("parent_task_id") != task_id:
            continue
        task = _task(data)
        if task.role != "analysis" or task.analysis_outline != outline or task.evidence_ids or task.related_result_ids:
            msg = f"Replay cannot restore non-frozen or externally dependent exploration task: {task.id}"
            raise ValueError(msg)
        state = add_task(state, task)
    if len(state["tasks"]) - 1 < MIN_ANALYSIS_TASKS:
        msg = "Replay requires at least two frozen exploration tasks"
        raise ValueError(msg)
    return state


def _restore_results(state: BlackboardState, board: dict[str, object], task_id: str) -> BlackboardState:
    for value in _mapping(board["results"]).values():
        data = _mapping(value)
        if data.get("task_id") == task_id or data.get("task_id") not in state["tasks"]:
            continue
        result = _result(data)
        if result.analysis_protocol != PROTOCOL or (result.analysis_detail is not None and not isinstance(result.analysis_detail, ExplorationReport)):
            msg = f"Replay requires original exploration reports or explicit failures: {result.id}"
            raise ValueError(msg)
        state = add_result(state, result)
    if not any(isinstance(result.analysis_detail, ExplorationReport) for result in state["results"].values()):
        msg = "Replay requires at least one validated exploration report"
        raise ValueError(msg)
    return state


def _restore_workers(payload: dict[str, object], state: BlackboardState, task_id: str) -> dict[str, AnalysisExecution]:
    workers = {}
    for identity in state["tasks"]:
        if identity == task_id:
            continue
        original = TypeAdapter(AnalysisExecution).validate_python(_mapping(payload["worker_executions"])[identity])
        results = [result for result in state["results"].values() if result.task_id == identity]
        completed = original.status == "completed"
        if original.status not in ("completed", "failed", "stopped") or len(results) != 1:
            msg = f"Exploration has not terminated with exactly one report or failure: {identity}"
            raise ValueError(msg)
        if completed != isinstance(results[0].analysis_detail, ExplorationReport):
            msg = f"Worker status disagrees with frozen report: {identity}"
            raise ValueError(msg)
        workers[identity] = AnalysisExecution(status=original.status, error=original.error)
    return workers


def _restore_issues(value: object, workers: dict[str, AnalysisExecution], outline: VisualOutline) -> list[dict[str, object]]:
    issues = TypeAdapter(list[dict[str, object]]).validate_python(value)
    identities: set[str] = set()
    for issue in issues:
        identity = issue.get("id")
        elements = TypeAdapter(list[str]).validate_python(issue.get("element_ids", []))
        if not isinstance(identity, str) or identity in identities or issue.get("task_id") not in workers or issue.get("outline_version") != 1:
            msg = "Replay requires unique initial-outline issues belonging to frozen workers"
            raise ValueError(msg)
        if set(elements) - {element.id for element in outline.elements}:
            msg = f"Outline issue references unknown elements: {identity}"
            raise ValueError(msg)
        if any(not isinstance(issue.get(field), str) or not str(issue[field]).strip() for field in ("region", "description")):
            msg = f"Outline issue lacks a concrete region or description: {identity}"
            raise ValueError(msg)
        identities.add(identity)
    return issues


def load_replay_input(source: Path) -> ReplayInput:
    """校验原图及完整探索义务, 拒绝无法忠实恢复的快照.

    Args:
        source: 包含 run.json 和 reference.png 的既有运行目录.

    Returns:
        保持原任务、报告身份及问题的冻结输入.

    Raises:
        OSError: 必要制品不可读取.
        ValueError: 协议、原图哈希或冻结探索状态无效.
    """
    raw, reference = (source / "run.json").read_bytes(), (source / "reference.png").read_bytes()
    payload = _mapping(json.loads(raw))
    digest = hashlib.sha256(reference).hexdigest()
    if payload.get("analysis_protocol") != PROTOCOL or payload.get("kind") != "independent_exploration":
        msg = "Unsupported replay protocol"
        raise ValueError(msg)
    if payload.get("reference_sha256") != digest or not reference.startswith(b"\x89PNG\r\n\x1a\n"):
        msg = "Replay reference hash or PNG signature mismatch"
        raise ValueError(msg)
    outlines = TypeAdapter(list[VisualOutline]).validate_python(payload["outline_versions"])
    if not outlines:
        msg = "Missing frozen initial outline"
        raise ValueError(msg)
    outline = outlines[0]
    validate_outline(outline)
    board, task_id = _mapping(payload["blackboard"]), str(payload["task_id"])
    state = _restore_tasks(_root_state(board, task_id), board, task_id, outline)
    state = _restore_results(state, board, task_id)
    workers = _restore_workers(payload, state, task_id)
    return ReplayInput(
        state=state,
        task_id=task_id,
        outline=outline,
        workers=workers,
        issues=_restore_issues(payload["issues"], workers, outline),
        reference=reference,
        source_hash=hashlib.sha256(raw).hexdigest(),
        reference_hash=digest,
    )


def _write_manifest(session: ReplaySession, source: Path, frozen: ReplayInput) -> None:
    effective, origin = resolve_phase_options(session.options, "integration")
    package = Path(__file__).resolve().parents[1]
    fingerprints = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(package.rglob("*.py"))}
    payload = {
        "kind": "frozen_exploration_replay",
        "source_run": str(source.resolve()),
        "source_run_sha256": frozen.source_hash,
        "reference_sha256": frozen.reference_hash,
        "options": asdict(session.options),
        "effective_integration_output": {"max_output_tokens": effective.max_output_tokens, "source": origin},
        "source_fingerprints": fingerprints,
        "report_ids": list(session._require_store().report_ids),
        "initial_worker_statuses": {key: value.status for key, value in frozen.workers.items()},
        "initial_issue_count": len(frozen.issues),
    }
    (session.directory / "replay-manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def prepare_replay(source: Path, *, options: AnalysisOptions) -> ManagedIntegrationSession:
    """创建独立目录并重新导入原始报告, 不发送模型请求.

    Args:
        source: 既有运行目录.
        options: 本次回放的显式预算, 不继承原运行调用计数.

    Returns:
        仅包含初始探索库、问题和失败义务的新会话.
    """
    return _prepare_replay(source, options=options, session_type=ManagedIntegrationSession)


def _prepare_replay(source: Path, *, options: AnalysisOptions, session_type: type[_Session]) -> _Session:
    """复用同一冻结输入恢复器, 允许显式实验入口选择会话实现."""
    frozen = load_replay_input(source)
    directory = create_run_directory(options.output_dir)
    (directory / "reference.png").write_bytes(frozen.reference)
    state: BlackboardState = frozen.state
    target = state["targets"][state["tasks"][frozen.task_id].target_version]
    state = {**state, "targets": {target.version: replace(target, reference_path=str(directory / "reference.png"))}}
    reference_url = "data:image/png;base64," + base64.b64encode(frozen.reference).decode("ascii")
    session = session_type(state, frozen.task_id, options, directory, reference_url)
    session.outline, session.outline_versions = frozen.outline, [frozen.outline]
    session.issues, session.workers = deepcopy(frozen.issues), frozen.workers
    session.directions = [task.objective for task in state["tasks"].values() if task.parent_task_id == frozen.task_id]
    session.store = LibraryStore(frozen.outline, directory)
    for result in state["results"].values():
        if isinstance(result.analysis_detail, ExplorationReport):
            session.store.add_report(result.id, result.analysis_detail)
    _write_manifest(session, source, frozen)
    session.save()
    return session


def run_replay(source: Path, *, options: AnalysisOptions) -> AnalysisOutcome:
    """仅执行主整合并保存真实终态及错误, 不重新探索.

    Args:
        source: 完整冻结探索快照所在目录.
        options: 本次独立回放预算.

    Returns:
        主整合的实际结果和独立制品目录.
    """
    if options.max_main_calls < 1:
        msg = "Replay requires a positive main-call budget"
        raise ValueError(msg)
    session = prepare_replay(source, options=options)
    try:
        session._integrate(build_model())
    except KeyboardInterrupt:
        session.stop_reason, session.execution.status, session.execution.error = "interrupted", "stopped", "Interrupted by caller"
        raise
    except Exception as exc:  # noqa: BLE001  # 与正式入口一致, 保留服务失败制品而非丢失整个对照样本.
        session.stop_reason, session.execution.status, session.execution.error = "error", "failed", f"{type(exc).__name__}: {exc}"
    finally:
        session.finalize()
        session.save()
        session.event("run_finished", {"status": session.stop_reason, "error": session.execution.error})
    return session.outcome()
