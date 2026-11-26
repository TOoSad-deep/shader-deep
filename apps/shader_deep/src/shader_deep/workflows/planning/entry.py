"""从固定观察版本运行一个规划图并发布独立 M/S 包."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import asdict, replace
from time import perf_counter
from typing import TYPE_CHECKING

from langchain_core.callbacks import UsageMetadataCallbackHandler

from shader_deep.agents.planning.native import build_planning_agent, planning_input
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.planning_inputs import capture_planning_source
from shader_deep.infrastructure.storage.planning_package import write_planning_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.planning.contracts import PlanningOutcome, ScenePlanningResult
from shader_deep.workflows.planning.options import PlanningOptions
from shader_deep.workflows.planning.session import PlanningSession

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.agents.generation.options import GenerationOptions
    from shader_deep.workflows.planning.cascade import PlanningRevision

_MAX_FEEDBACK_ITEMS = 8
_MAX_FEEDBACK_LENGTH = 4096


def _record_metrics(session: PlanningSession, usage: UsageMetadataCallbackHandler, started: float) -> None:
    values = usage.usage_metadata
    totals = {key: sum(value[key] for value in values.values()) for key in ("input_tokens", "output_tokens", "total_tokens")} if values else None
    session.record_runtime(
        {
            "elapsed_seconds": perf_counter() - started,
            "elapsed_scope": "planning_before_publication",
            "token_usage": totals,
            "usage_scope": "sdk_reported_responses_only",
            "http_request_count": None,
        }
    )


def _invoke(session: PlanningSession, usage: UsageMetadataCallbackHandler, model: BaseChatModel | None) -> None:
    graph = build_planning_agent(model if model is not None else build_model(), session, session.options)
    graph.invoke(
        planning_input(session),
        config={
            "run_name": "scene-planner",
            "recursion_limit": session.options.max_model_calls * 4 + 10,
            "callbacks": [usage],
            "metadata": {"planning_run_id": session.directory.name, "observation_sha256": session.source.observation_sha256},
        },
    )


def _publish(session: PlanningSession, result: ScenePlanningResult) -> tuple[ScenePlanningResult, Path | None]:
    if result.submission is None:
        return result, None
    try:
        directory = write_planning_package(session.directory / "report", session.source, result.submission)
    except Exception as error:  # noqa: BLE001  # 正式发布失败仍交付已接受快照和 failed 索引.
        reason = f"publish_error: {type(error).__name__}: {error}"
        return replace(result, status="failed", error=f"{result.error}; {reason}" if result.error else reason), None
    return result, directory


def _deliver(session: PlanningSession, result: ScenePlanningResult) -> PlanningOutcome:
    result, package = _publish(session, result)
    options = {**asdict(session.options), "output_dir": str(session.options.output_dir) if session.options.output_dir is not None else None}
    details = {
        "planning_protocol": "scene_planning_v1",
        "status": result.status,
        "error": result.error,
        "report_path": "report" if package is not None else None,
        "options": options,
        "execution": result.execution,
    }
    diagnostic_error: OSError | None = None
    try:
        session.save_manifest()
    except OSError as error:
        diagnostic_error = error
        reason = f"diagnostic_error: manifest.json: {error}"
        result = replace(result, error=f"{result.error}; {reason}" if result.error else reason)
        details["error"] = result.error
    try:
        write_snapshot(session.directory / "result.json", details)
    except OSError:
        if diagnostic_error is not None:
            raise diagnostic_error from None
        raise
    log_analysis(
        session.directory, "规划运行结束", details={"status": result.status, "report_path": str(package) if package else None, "error": result.error}
    )
    return PlanningOutcome(
        run_dir=session.directory,
        report_dir=package,
        status=result.status,
        error=result.error,
        execution=result.execution,
        observation_revision_request=result.observation_revision_request,
    )


def _execute(session: PlanningSession, model: BaseChatModel | None = None) -> PlanningOutcome:
    started, usage = perf_counter(), UsageMetadataCallbackHandler()
    failure: str | None = None
    interrupted: KeyboardInterrupt | None = None
    try:
        _invoke(session, usage, model)
    except KeyboardInterrupt as error:
        interrupted, failure = error, "规划运行被用户中断"
    except Exception as error:  # noqa: BLE001  # 图异常后仍保留合法接受快照与失败信息.
        failure = f"{type(error).__name__}: {error}"
        log_analysis(session.directory, "规划图执行失败", level=logging.ERROR, details={"error": failure})
    _record_metrics(session, usage, started)
    try:
        outcome = _deliver(session, session.finalize(failure))
    except Exception:
        if interrupted is None:
            raise
        with suppress(OSError):
            session.save_manifest()
        raise interrupted from None
    if interrupted is not None:
        raise interrupted
    return outcome


def run_planning(
    observation_dir: Path,
    request: str,
    *,
    options: PlanningOptions | None = None,
    generation_options: GenerationOptions | None = None,
    model: BaseChatModel | None = None,
    revision_feedback: tuple[str, ...] = (),
    revision: PlanningRevision | None = None,
) -> PlanningOutcome:
    """从已发布全图观察启动单个 M/S 规划 Agent.

    !!! warning "实验性接口"
        scene_planning_v1 固定绑定观察版本, 全部方案覆盖同一完整场景.

    Args:
        observation_dir: scene_observations_v1 观察包目录.
        request: 本次整图要求.
        options: 规划调用、传输与输出目录配置.
        generation_options: 预期生成尺寸与时间, 省略时使用原图尺寸与时间零.
        model: 可注入模型, 省略时加载应用配置.
        revision: 定向复核后的旧规划草稿与可改来源 ID.
        revision_feedback: 可选的前轮局部试验或方法修订证据, 不覆盖用户要求.

    Returns:
        规划状态、运行目录与成功发布的正式包索引.

    Raises:
        ValueError: 要求、观察包或生成约束无效.
        OSError: 固定输入或最终结果无法保存.
    """
    if not isinstance(request, str) or not request.strip():
        msg = "规划要求必须为非空文本"
        raise ValueError(msg)
    if (
        isinstance(revision_feedback, str)
        or len(revision_feedback) > _MAX_FEEDBACK_ITEMS
        or any(not isinstance(item, str) or not item.strip() or len(item) > _MAX_FEEDBACK_LENGTH for item in revision_feedback)
    ):
        msg = f"方法修订证据须为至多 {_MAX_FEEDBACK_ITEMS} 条非空文本, 每条最多 {_MAX_FEEDBACK_LENGTH} 字符"
        raise ValueError(msg)
    selected = options or PlanningOptions()
    directory = create_run_directory(selected.output_dir)
    source = capture_planning_source(observation_dir, directory, request, generation_options=generation_options)
    feedback = tuple(revision_feedback)
    if feedback:
        write_snapshot(directory / "inputs/revision_feedback.json", {"revision_feedback": list(feedback)})
    return _execute(PlanningSession(source, selected, revision_feedback=feedback, revision=revision), model)
