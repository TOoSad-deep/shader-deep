"""单 Agent 全图观察的公开入口与独立交付核查."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.domain.tasks import ResultRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.observation_package import (
    ObservationManifest,
    ObservationReference,
    read_observation_package,
    write_observation_package,
)
from shader_deep.infrastructure.storage.planning_inputs import reference_size
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.analysis.pipeline import execute_scene_analysis
from shader_deep.workflows.options import AnalysisOptions, observation_option_diagnostics, validate_observation_options
from shader_deep.workflows.outcomes import AnalysisOutcome

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from pydantic import JsonValue

    from shader_deep.domain.scene_observations import ReferenceMeasurement
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.workflows.analysis.contracts import AnalysisRevision, SceneAnalysisResult
    from shader_deep.workflows.analysis.session import AnalysisSession

PROTOCOL = "scene_observations_v1"


def _validate_task(state: BlackboardState, task_id: str, options: AnalysisOptions) -> TaskRecord:
    validate_observation_options(options)
    task = state["tasks"][task_id]
    if task.role != "analysis" or task.lens_config is not None or task.parent_task_id is not None or not task.objective.strip():
        msg = "Expected a root analysis task with a nonblank objective"
        raise ValueError(msg)
    if any(child.parent_task_id == task.id for child in state["tasks"].values()) or task.related_result_ids:
        msg = "全图观察需要原始参考图和新的根任务, 不自动续跑或转换历史报告"
        raise ValueError(msg)
    return task


def _analysis_request(target: TargetRecord, task: TaskRecord) -> str:
    """保留用户要求与约束, 不以任务摘要替换原始目标."""
    parts = [target.request]
    if target.constraints:
        parts.append("明确约束: " + "; ".join(target.constraints))
    if target.protected_features:
        parts.append("需保护特征: " + "; ".join(target.protected_features))
    if task.objective != target.request:
        parts.append("本次分析目标: " + task.objective)
    return "\n".join(parts)


def _delivery(directory: Path, reference: bytes, result: SceneAnalysisResult) -> None:
    """只发布已接受的观察快照; 失败由调用方统一转为公开终态."""
    if result.observations is None or result.submission is None or result.status == "failed":
        return
    manifest = ObservationManifest(
        reference=ObservationReference(sha256=hashlib.sha256(reference).hexdigest()),
        status="partial" if result.submission.gaps else "completed",
        context_notes=result.submission.context_notes,
        open_questions=result.submission.open_questions,
        gaps=result.submission.gaps,
        reference_measurements=result.reference_measurements,
    )
    write_observation_package(directory / "report", result.observations, manifest, reference)
    log_analysis(
        directory, "报告包已发布", details={"status": manifest.status, "report_dir": str(directory / "report"), "analysis_protocol": PROTOCOL}
    )


def _published_result(directory: Path, reference: bytes, result: SceneAnalysisResult) -> SceneAnalysisResult:
    try:
        _delivery(directory, reference, result)
    except Exception as error:  # noqa: BLE001  # 发布异常必须留下 failed 索引, 接受快照仍由会话保存.
        reason = f"publish_error: {type(error).__name__}: {error}"
        return replace(result, status="failed", error=reason if result.error is None else result.error + "; " + reason)
    return result


def _configuration(options: AnalysisOptions) -> dict[str, object]:
    """仅列实际生效参数, 弃用占位不冒充角色数或无限预算."""
    active = ("max_model_calls", "max_output_tokens", "max_request_retries", "request_timeout_seconds", "stream_model_responses")
    values = asdict(options)
    return {**{name: values[name] for name in active}, "output_dir": str(options.output_dir) if options.output_dir is not None else None}


def _summary(state: BlackboardState, task_id: str, directory: Path, result: SceneAnalysisResult) -> tuple[BlackboardState, ResultRecord | None]:
    if result.status == "failed" or not (directory / "report").is_dir():
        return state, None
    record = ResultRecord(
        id=directory.name + "-scene-observations",
        task_id=task_id,
        status="completed" if result.status == "completed" else "partial",
        summary="全图 E/F/R 观察包已交付: report/manifest.json; 观察完成不代表 M/S 规划或视觉验收完成",
        analysis_protocol=PROTOCOL,
    )
    return add_result(state, record), record


def _outcome(state: BlackboardState, task_id: str, directory: Path, result: SceneAnalysisResult, options: AnalysisOptions) -> AnalysisOutcome:
    state, summary = _summary(state, task_id, directory, result)
    package = directory / "report" if summary is not None else None
    details = {
        "analysis_protocol": PROTOCOL,
        "stop_reason": result.status,
        "error": result.error,
        "report_path": "report" if package is not None else None,
        "options": _configuration(options),
        "deprecated_option_diagnostics": observation_option_diagnostics(options),
        "execution": result.task_state,
    }
    write_snapshot(directory / "result.json", details)
    save_run(directory, state, details)
    return AnalysisOutcome(state=state, task_id=task_id, summary_result=summary, run_dir=directory, stop_reason=result.status, report_dir=package)


def _capture_revision(revision: AnalysisRevision, reference: bytes) -> tuple[dict[str, JsonValue], tuple[ReferenceMeasurement, ...]]:
    manifest, observations = read_observation_package(revision.observation_dir)
    if manifest.reference.sha256 != hashlib.sha256(reference).hexdigest():
        msg = "定向复核必须使用旧观察绑定的同一参考 PNG"
        raise ValueError(msg)
    request = type(revision.request).model_validate(revision.request.model_dump(mode="json"))
    request.validate_against(observations, reference_size(reference))
    context = {
        "request": request.model_dump(mode="json"),
        "previous_observations": observations.model_dump(mode="json"),
        "previous_manifest": manifest.model_dump(mode="json"),
    }
    return context, manifest.reference_measurements


def run_analysis_task(
    state: BlackboardState,
    task_id: str,
    *,
    asset_root: Path | None = None,
    options: AnalysisOptions | None = None,
    revision: AnalysisRevision | None = None,
    model: BaseChatModel | None = None,
) -> AnalysisOutcome:
    """执行单 Agent 全图 E/F/R 观察并交付可回读文件包.

    !!! warning "实验性接口"
        默认协议改为 scene_observations_v1; 旧多角色非默认配置需要迁移.

    Args:
        state: 已登记根分析任务的黑板.
        task_id: 本轮新的根分析任务标识.
        asset_root: 相对参考图路径的根目录.
        options: 单角色调用、输出和传输预算.
        revision: 从同一原图的旧观察包开展定向复核.
        model: 可注入模型, 省略时加载应用配置.

    Returns:
        本轮终态与文件包索引, 无合法交付时 report_dir 为空.

    Raises:
        ValueError: 任务、配置、原图或模型配置无效.
        OSError: 必需文件或最终诊断无法读写.
    """
    options = options or AnalysisOptions()
    task = _validate_task(state, task_id, options)
    target = state["targets"][task.target_version]
    root = asset_root if asset_root is not None else Path.cwd()
    reference_url = png_data_url(root / target.reference_path)
    reference = base64.b64decode(reference_url.split(",", 1)[1])
    revision_context, inherited_measurements = _capture_revision(revision, reference) if revision is not None else (None, ())
    selected_model = model if model is not None else build_model()
    directory = create_run_directory(options.output_dir)
    (directory / "reference.png").write_bytes(reference)
    if revision_context is not None:
        write_snapshot(directory / "observation_revision_input.json", revision_context)
    outcomes: list[AnalysisOutcome] = []

    def deliver(final: SceneAnalysisResult, _session: AnalysisSession) -> SceneAnalysisResult:
        published = _published_result(directory, reference, final)
        outcomes.append(_outcome(state, task_id, directory, published, options))
        return published

    result = execute_scene_analysis(
        _analysis_request(target, task),
        reference_url,
        options,
        directory,
        model=selected_model,
        on_delivery=deliver,
        revision_context=revision_context,
        inherited_measurements=inherited_measurements,
    )
    return outcomes[-1] if outcomes else _outcome(state, task_id, directory, result, options)


def run_analysis(
    path: Path, prompt: str, *, options: AnalysisOptions | None = None, revision: AnalysisRevision | None = None, model: BaseChatModel | None = None
) -> AnalysisOutcome:
    """从固定 PNG 与要求启动一次全图观察.

    Args:
        path: 本地 PNG 参考图.
        prompt: 用户要求与观察目标.
        options: 可选的单角色运行参数.
        revision: 可选的只读旧观察和结构化复核请求.
        model: 可注入模型, 省略时加载应用配置.

    Returns:
        状态、黑板结果索引与可选观察报告目录.

    Raises:
        ValueError: 要求为空或配置尚未迁移.
    """
    if not prompt.strip():
        msg = "Prompt must not be empty"
        raise ValueError(msg)
    state = add_target(new_blackboard(), TargetRecord(version="T1", request=prompt, reference_path=str(path.resolve())))
    state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective=prompt))
    return run_analysis_task(state, "A1", options=options, revision=revision, model=model)
