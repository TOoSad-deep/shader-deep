"""固定双图与任务标准, 运行一次原生评审并发布报告."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import asdict, replace
from time import perf_counter
from typing import TYPE_CHECKING

from langchain_core.callbacks import UsageMetadataCallbackHandler

from shader_deep.agents.review.native import build_review_agent, review_input
from shader_deep.domain.review_coverage import coverage_only_submission
from shader_deep.domain.visual_review import derive_verdict
from shader_deep.imaging.review_evidence import WorkflowEvidence
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.review_package import capture_review_source, capture_workflow_review_source, write_review_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.review.contracts import ReviewOutcome, ReviewResult
from shader_deep.workflows.review.options import ReviewOptions
from shader_deep.workflows.review.publication import deliver_accepted_review, publication_lock
from shader_deep.workflows.review.session import ReviewSession

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shader_deep.domain.scene_observations import ReferenceMeasurement
    from shader_deep.domain.visual_review import BoundReviewCriteria, PixelRegion, ReviewRequest, ReviewSource
    from shader_deep.infrastructure.storage.review_package import CapturedReviewSource


def _record_metrics(session: ReviewSession, usage: UsageMetadataCallbackHandler, started: float) -> None:
    values = usage.usage_metadata
    totals = {key: sum(value[key] for value in values.values()) for key in ("input_tokens", "output_tokens", "total_tokens")} if values else None
    session.record_runtime(
        {
            "elapsed_seconds": perf_counter() - started,
            "elapsed_scope": "review_before_publication",
            "token_usage": totals,
            "usage_scope": "sdk_reported_responses_only",
            "http_request_count": None,
            "cost": None,
            "cost_status": "unknown",
        }
    )


def _invoke(session: ReviewSession, usage: UsageMetadataCallbackHandler) -> None:
    if session.source.request.protocol == "visual_review_v2":
        if all(check.unassessed for check in session.checks):
            session.finish("所有必查项均缺少完整视图。")
            return
    else:
        uncovered = coverage_only_submission(session.source.criteria, session.source.coverage)
        if uncovered is not None:
            session.submit(uncovered)
            return
    graph = build_review_agent(build_model(), session, session.options)
    graph.invoke(
        review_input(session),
        config={
            "run_name": "visual-reviewer",
            "recursion_limit": session.options.max_model_calls * 4 + 10,
            "callbacks": [usage],
            "metadata": {"review_run_id": session.directory.name, "criteria_sha256": session.source.criteria.criteria_sha256},
        },
    )


def _publish(session: ReviewSession, result: ReviewResult) -> tuple[ReviewResult, Path | None]:
    if result.submission is None:
        return result, None
    try:
        directory = write_review_package(
            session.directory / "report",
            session.source,
            result.submission,
            metadata=session.execution(),
            check_answers=session.check_answers if session.source.request.protocol == "visual_review_v2" else None,
        )
    except Exception as error:  # noqa: BLE001  # 正式发布失败仍保留接受快照和 failed 索引.
        reason = f"publish_error: {type(error).__name__}: {error}"
        return replace(result, status="failed", error=f"{result.error}; {reason}" if result.error else reason), None
    return result, directory


def _deliver(session: ReviewSession, result: ReviewResult) -> ReviewOutcome:
    if session.source.request.protocol == "visual_review_v2" and result.submission is not None:
        outcome = deliver_accepted_review(session, result)
        log_analysis(session.directory, "评审运行结束", details={"status": outcome.status, "verdict": outcome.verdict, "error": outcome.error})
        return outcome
    result, package = _publish(session, result)
    verdict = derive_verdict(result.submission) if package is not None and result.submission is not None else None
    options = {**asdict(session.options), "output_dir": str(session.options.output_dir) if session.options.output_dir is not None else None}
    details = {
        "review_protocol": session.source.request.protocol,
        "status": result.status,
        "verdict": verdict,
        "error": result.error,
        "report_path": "report" if package is not None else None,
        "options": options,
        "execution": session.execution(),
    }
    session.save_manifest()
    write_snapshot(session.directory / "result.json", details)
    log_analysis(session.directory, "评审运行结束", details={"status": result.status, "verdict": verdict, "error": result.error})
    return ReviewOutcome(
        run_dir=session.directory,
        report_dir=package,
        status=result.status,
        verdict=verdict,
        error=result.error,
        execution=session.execution(),
    )


def _execute(session: ReviewSession) -> ReviewOutcome:
    started, usage = perf_counter(), UsageMetadataCallbackHandler()
    failure: str | None = None
    interrupted: KeyboardInterrupt | None = None
    try:
        _invoke(session, usage)
    except KeyboardInterrupt as error:
        interrupted, failure = error, "评审运行被用户中断"
    except Exception as error:  # noqa: BLE001  # 图异常后核对接受事实并交付原始错误.
        failure = f"{type(error).__name__}: {error}"
        log_analysis(session.directory, "评审图执行失败", level=logging.ERROR, details={"error": failure})
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


def run_review(reference_path: Path, rendered_path: Path, request: ReviewRequest, *, options: ReviewOptions | None = None) -> ReviewOutcome:
    """对固定参考与候选渲染运行独立视觉评审.

    !!! warning "实验性接口"
        两种评审协议均固定本次层级模板、检查内容和双图身份. 默认整份提交, V2 按必查项登记答案.

    Args:
        reference_path: 参考 PNG 文件.
        rendered_path: 指定候选的实际 PNG 渲染图.
        request: 评价前明确的范围、目标与六维检查说明, 可选区域测量和视图覆盖检查.
        options: 评审预算、传输配置与输出目录.

    Returns:
        独立报告的运行状态、视觉结论和成功发布的正式包位置.

    Raises:
        ValueError: 启动前输入或固定材料捕获无效.
        OSError: 运行目录、固定副本或最终结果无法保存.
    """
    selected = options or ReviewOptions()
    directory = create_run_directory(selected.output_dir)
    try:
        source = capture_review_source(reference_path, rendered_path, directory, request)
    except (TypeError, ValueError) as error:
        msg = f"评审输入准备失败: {error}"
        raise ValueError(msg) from error
    return _run_captured(source, selected)


def run_workflow_review(
    reference_path: Path,
    rendered_path: Path,
    criteria: BoundReviewCriteria,
    *,
    options: ReviewOptions | None = None,
    reference_measurements: Sequence[ReferenceMeasurement] = (),
    optimization_regions: Sequence[PixelRegion] = (),
    source: ReviewSource | None = None,
    expand_views: bool = False,
    resample_regions: Sequence[PixelRegion] = (),
    rendered_time: float | None = None,
) -> ReviewOutcome:
    """执行门禁 A 已冻结的工作流 V2 盲评, 按需装配采样与角色栏.

    Args:
        reference_path: 本轮相同坐标系中的固定参考 PNG.
        rendered_path: 候选的渲染 PNG.
        criteria: 人工批准并冻结的 V2 标准.
        options: 评审运行预算、传输配置和输出父目录.
        reference_measurements: 本参考 PNG 的上游像素锚点, 使用参考原图坐标.
        optimization_regions: 优化器中目标或保护区, 共享画布坐标.
        source: 本次候选和运行的来源标签.
        resample_regions: 本次复评在共享画布上新增的成对采样区.
        expand_views: 补采时展开至输入 PNG 的既有范围, 保留共享坐标映射.
        rendered_time: 经人工选择重渲染时, 候选实际使用的固定时间.

    Returns:
        按 V2 逐项提交或程序短路派生的运行状态与结论.

    Raises:
        ValueError: 冻结材料或采样身份不匹配.
        OSError: 固定材料或报告无法保存.
    """
    selected = options or ReviewOptions()
    directory = create_run_directory(selected.output_dir)
    evidence = WorkflowEvidence(
        reference_measurements=tuple(reference_measurements),
        optimization_regions=tuple(optimization_regions),
        expand_views=expand_views,
        resample_regions=tuple(resample_regions),
        rendered_time=rendered_time,
    )
    try:
        captured = capture_workflow_review_source(reference_path, rendered_path, directory, criteria, evidence, source=source)
    except (TypeError, ValueError) as error:
        msg = f"评审输入准备失败: {error}"
        raise ValueError(msg) from error
    return _run_captured(captured, selected)


def _run_captured(source: CapturedReviewSource, options: ReviewOptions) -> ReviewOutcome:
    session = ReviewSession(source, options)
    if source.request.protocol == "visual_review_v2":
        with publication_lock(source.directory):
            return _execute(session)
    return _execute(session)
