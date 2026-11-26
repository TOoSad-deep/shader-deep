"""在共同固定条件下执行两套生成方案, 保存独立产物供人工比较."""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import new_blackboard
from shader_deep.domain.generation_comparison import (
    PLAN_COUNT,
    ComparisonItem,
    GenerationComparison,
    GenerationPlan,
    PlanningComparisonItem,
    PlanningGenerationComparison,
    validate_generation_plans,
)
from shader_deep.domain.visual_review import ReviewSource
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.generation_comparison import write_generation_comparison, write_planning_generation_comparison
from shader_deep.infrastructure.storage.generation_inputs import _reuse_generation_inputs
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.optimization.records import selected_optimization
from shader_deep.workflows.generation import _run_session
from shader_deep.workflows.generation_from_planning import _prepare_planning_generation
from shader_deep.workflows.generation_from_report import PreparedGeneration, _prepare_generation, _register_task, _save_prepared
from shader_deep.workflows.review import ReviewOptions, freeze_workflow_review, run_workflow_review

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextvars import Context

    from langchain_core.language_models import BaseChatModel

    from shader_deep.domain.local_probe import LocalProbeBinding, VerifiedSnippet
    from shader_deep.domain.scene_observations import ReferenceMeasurement
    from shader_deep.domain.tasks import BlackboardState, CandidateRecord
    from shader_deep.domain.visual_review import BoundReviewCriteria
    from shader_deep.infrastructure.storage.planning_package import PlanningPackage
    from shader_deep.workflows.generation_from_planning import PreparedPlanningGeneration


def _prepare_companion(
    first: PreparedGeneration, plan: GenerationPlan, state: BlackboardState | None, request: str, background: str
) -> PreparedGeneration:
    directory = create_run_directory(first.run_dir.parent)
    inputs = _reuse_generation_inputs(first.inputs, directory, plan.sketch_id, alternative=plan.alternative)
    # 从调用方原始黑板建立第二项; 第一项的新任务、候选和结果不进入它的业务输入.
    board, task_id = _register_task(state if state is not None else new_blackboard(), inputs, request, background)
    prepared = PreparedGeneration(run_dir=directory, state=board, task_id=task_id, inputs=inputs, options=first.options, asset_root=first.asset_root)
    _save_prepared(prepared)
    return prepared


def _run_plan(plan: GenerationPlan, prepared: PreparedGeneration, model: BaseChatModel) -> ComparisonItem:
    try:
        outcome = _run_session(
            prepared.state,
            prepared.task_id,
            prepared.asset_root,
            prepared.options,
            run_dir=prepared.run_dir,
            inputs=prepared.inputs,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 - 模型与渲染的运行异常在此转成单项结果, 另一项仍须执行.
        # 只隔离普通执行失败; KeyboardInterrupt 等取消继续向上传播, 不派发下一项.
        return _failed_item(plan, prepared, exc)
    candidate = outcome.selected_candidate
    return ComparisonItem(
        plan=plan,
        run_dir=str(outcome.run_dir),
        task_id=prepared.task_id,
        stop_reason=outcome.stop_reason,
        selected_candidate_id=candidate.id if candidate is not None else None,
        code_path=candidate.code_path if candidate is not None else None,
        preview_path=candidate.preview_path if candidate is not None else None,
    )


def _failed_item(plan: GenerationPlan, prepared: PreparedGeneration, error: Exception) -> ComparisonItem:
    item = ComparisonItem(
        plan=plan, run_dir=str(prepared.run_dir), task_id=prepared.task_id, stop_reason="error", error=f"{type(error).__name__}: {error}"
    )
    try:
        # 关闭资源失败可能发生在选择之后; 只读取已落盘索引, 保留真实产物导航.
        snapshot = json.loads((prepared.run_dir / "run.json").read_text(encoding="utf-8"))
        selected = snapshot["selected_candidate_id"]
        if selected is None:
            return item
        candidate = snapshot["blackboard"]["candidates"][selected]
        return replace(item, selected_candidate_id=selected, code_path=candidate["code_path"], preview_path=candidate["preview_path"])
    except (OSError, ValueError, KeyError, TypeError):
        # 原始失败也可能影响快照读取, 仍保留原异常并继续另一项, 不恢复或重跑会话.
        return item


def _comparison(
    directory: Path, first: PreparedGeneration, model_name: str, request: str, background: str, items: tuple[ComparisonItem, ...]
) -> GenerationComparison:
    binding, options = first.inputs.binding, first.options
    return GenerationComparison(
        directory=str(directory),
        report_sha256=binding.content_sha256,
        reference_sha256=binding.reference_sha256,
        element_id=binding.element_id,
        request=request,
        background=background,
        baseline_id=first.inputs.baseline.candidate_id if first.inputs.baseline is not None else None,
        model_name=model_name,
        width=options.width,
        height=options.height,
        time=options.time,
        max_attempts=options.max_attempts,
        items=items,
    )


def run_generation_comparison(
    report_dir: Path,
    plans: Sequence[GenerationPlan],
    request: str,
    *,
    background: str,
    state: BlackboardState | None = None,
    baseline_id: str | None = None,
    asset_root: Path | None = None,
    width: int | None = None,
    height: int | None = None,
    time: float = 0.0,
    max_attempts: int = 3,
    output_dir: Path | None = None,
) -> GenerationComparison:
    """在共同条件下顺序执行两项显式方案, 保存比较索引而不自动选择胜者.

    !!! warning "实验性接口"
        首版固定比较同一元素的两项方案, 不进行自动评分、并发执行或失败重跑.

    Args:
        report_dir: 同一元素的完整五库报告目录.
        plans: 恰好两项明确命名的草图选择, 每项可指定一个局部备选.
        request: 两项共用的用户执行要求.
        background: 两项共用的背景要求.
        state: 可选的已有黑板, 两项独立引用其原始记录.
        baseline_id: 两项共用的起始候选标识.
        asset_root: 已有候选相对制品路径的根目录.
        width: 显式渲染宽度, 未提供时使用固定原图宽度.
        height: 显式渲染高度, 未提供时使用固定原图高度.
        time: 两项固定的 iTime.
        max_attempts: 每项独立的渲染次数上限.
        output_dir: 比较目录的父目录.

    Returns:
        共同条件、两项实际结果及比较目录; 人工选择保持为空.

    Raises:
        ValueError: 方案数量、输入、选择、渲染或模型配置无效.
        OSError: 固定材料或比较索引无法读取或写入.
    """
    selected = tuple(plans)
    validate_generation_plans(selected)
    options = GenerationOptions(width=512 if width is None else width, height=512 if height is None else height, time=time, max_attempts=max_attempts)
    directory = create_run_directory(output_dir)
    try:
        first = _prepare_generation(
            report_dir,
            selected[0].sketch_id,
            request,
            background=background,
            alternative=selected[0].alternative,
            state=state,
            baseline_id=baseline_id,
            asset_root=asset_root,
            options=replace(options, output_dir=directory / "runs"),
            size=(width, height),
        )
        second = _prepare_companion(first, selected[1], state, request, background)
        # 创建一次客户端固定模型配置; 两项仍各自创建 Agent 与历史, 此处尚未请求模型.
        model = build_model()
    except BaseException:
        # 准备失败还没有运行结果, 只清理本批独占目录, 不生成虚假的失败条目.
        shutil.rmtree(directory)
        raise
    items = tuple(_run_plan(plan, prepared, model) for plan, prepared in zip(selected, (first, second), strict=True))
    comparison = _comparison(directory, first, model.model_name, request, background, items)
    write_generation_comparison(comparison)
    return comparison


def _shared_criteria(probes: tuple[LocalProbeBinding, ...], criteria: BoundReviewCriteria | None) -> BoundReviewCriteria | None:
    if probes:
        if len(probes) != PLAN_COUNT:
            msg = "局部对照需要为每项提供一个局部绑定"
            raise ValueError(msg)
        ignored = {"sketch_id", "alternative_index", "mechanism_ids", "dependency_sha256"}
        if probes[0].model_dump(exclude=ignored) != probes[1].model_dump(exclude=ignored):
            msg = "局部对照两项须使用同一试验目标、上下文、预算和冻结标准"
            raise ValueError(msg)
        if criteria is not None and criteria != probes[0].criteria:
            msg = "显式评审标准与局部绑定不一致"
            raise ValueError(msg)
        criteria = probes[0].criteria
    elif criteria is not None and criteria.request.scope != "scene":
        msg = "整图对照使用整图评审标准"
        raise ValueError(msg)
    if criteria is not None and freeze_workflow_review(criteria.request) != criteria:
        msg = "对照须使用完整、有效的 V2 冻结标准"
        raise ValueError(msg)
    return criteria


def _comparison_review(
    item: PlanningComparisonItem,
    prepared: PreparedPlanningGeneration,
    candidate: CandidateRecord,
    criteria: BoundReviewCriteria,
    options: ReviewOptions,
    reference_measurements: tuple[ReferenceMeasurement, ...],
) -> PlanningComparisonItem:
    reference = Path(prepared.inputs.reference_path)
    if candidate.preview_path is None:
        return replace(item, review_status="blocked", review_error="已选候选没有渲染预览")
    try:
        optimized = selected_optimization(prepared.run_dir, candidate.id)
        outcome = run_workflow_review(
            reference,
            Path(candidate.preview_path),
            criteria,
            options=options,
            reference_measurements=reference_measurements,
            optimization_regions=tuple(dict.fromkeys(optimized.evidence_regions)) if optimized is not None else (),
            source=ReviewSource(candidate_id=candidate.id, run_id=prepared.run_dir.name),
            rendered_time=prepared.inputs.options.time,
        )
    except Exception as error:  # noqa: BLE001  # 单项运行失败需保留另一项结果和本项生成产物.
        return replace(item, review_status="failed", review_error=f"{type(error).__name__}: {error}", review_calls=options.max_model_calls)
    calls = outcome.execution.get("model_calls")
    return replace(
        item,
        review_run_dir=str(outcome.run_dir),
        review_report_dir=str(outcome.report_dir) if outcome.report_dir is not None else None,
        review_status=outcome.status,
        review_verdict=outcome.verdict,
        review_error=outcome.error,
        review_calls=calls if type(calls) is int else options.max_model_calls,
    )


def _run_planning_plan(
    plan: GenerationPlan,
    prepared: PreparedPlanningGeneration,
    model: BaseChatModel,
    criteria: BoundReviewCriteria | None,
    review_options: ReviewOptions,
    reference_measurements: tuple[ReferenceMeasurement, ...],
) -> PlanningComparisonItem:
    item = PlanningComparisonItem(plan=plan, run_dir=str(prepared.run_dir), task_id="G1", stop_reason="error")
    try:
        outcome = _run_session(
            prepared.state,
            "G1",
            Path.cwd().resolve(),
            prepared.inputs.options,
            run_dir=prepared.run_dir,
            inputs=prepared.inputs,
            model=model,
        )
    except Exception as error:  # noqa: BLE001  # 一项生成为独立运行, 失败不阻止另一项.
        return replace(item, error=f"{type(error).__name__}: {error}", generation_calls=prepared.inputs.options.resolved_max_model_calls)
    candidate = outcome.selected_candidate
    item = replace(item, stop_reason=outcome.stop_reason, generation_calls=outcome.model_calls)
    if candidate is None or outcome.stop_reason != "completed":
        return item
    item = replace(
        item,
        selected_candidate_id=candidate.id,
        code_path=candidate.code_path,
        preview_path=candidate.preview_path,
        render_record_path=candidate.render_record_path,
        baked_code_path=str(outcome.baked_code_path) if outcome.baked_code_path is not None else None,
    )
    return item if criteria is None else _comparison_review(item, prepared, candidate, criteria, review_options, reference_measurements)


def _planning_comparison_options(
    package: PlanningPackage, options: GenerationOptions | None, probes: tuple[LocalProbeBinding, ...]
) -> GenerationOptions:
    if options is not None:
        return options
    if probes:
        probe = probes[0]
        return GenerationOptions(width=probe.width, height=probe.height, time=probe.time, max_attempts=probe.max_attempts, optimize_parameters=True)
    fixed = package.source.constraints
    return GenerationOptions(width=fixed.width, height=fixed.height, time=fixed.time, optimize_parameters=True)


def _validate_prepared_comparison(
    prepared: tuple[PreparedPlanningGeneration, ...], package: PlanningPackage, models: tuple[BaseChatModel, BaseChatModel]
) -> str:
    if any(item.inputs.binding.content_sha256 != package.content_sha256 for item in prepared):
        msg = "共同规划在对照输入捕获期间发生变化"
        raise ValueError(msg)
    identifier = _comparison_model_name(models[0])
    if identifier != _comparison_model_name(models[1]):
        msg = "两项生成须采用同一模型配置"
        raise ValueError(msg)
    return identifier


def _comparison_model_name(model: BaseChatModel) -> str:
    identifier = getattr(model, "model_name", None)
    return identifier if isinstance(identifier, str) and identifier else model._llm_type


def run_planning_generation_comparison(
    planning_dir: Path,
    plans: Sequence[GenerationPlan],
    *,
    options: GenerationOptions | None = None,
    criteria: BoundReviewCriteria | None = None,
    local_probes: Sequence[LocalProbeBinding] = (),
    review_options: ReviewOptions | None = None,
    verified_snippets: Sequence[VerifiedSnippet] = (),
    model: BaseChatModel | None = None,
) -> PlanningGenerationComparison:
    """在同一固定参考、渲染约束下并行执行两套方案, 供人工对照.

    !!! warning "实验性接口"
        两项独立维护会话. 注入的生成模型需支持并行调用.

    Args:
        planning_dir: 同一正式整图规划包目录.
        plans: 两项不重复的显式方案, 每项可选择一项局部备选.
        options: 共同生成与渲染条件, 省略时继承固定输入约束.
        criteria: 可选的共同冻结标准, 保留各自独立的 V2 评审报告.
        local_probes: 可选的两项局部绑定, 只允许切换方案与对应机制.
        review_options: 同一评审预算, 报告保存到本次对照目录.
        verified_snippets: 与两套整图方案均兼容的共同已验证片段. 局部对照不接受片段.
        model: 可选的共同生成模型, 省略时分别构造等配置的客户端. 评审使用独立会话与默认模型配置.

    Returns:
        同一执行条件下各自独立的两项结果和材料位置.

    Raises:
        ValueError: 方案、固定材料、局部条件或评审标准无效.
        OSError: 对照索引或输入捕获无法保存.
    """
    selected, probes, snippets = tuple(plans), tuple(local_probes), tuple(verified_snippets)
    validate_generation_plans(selected)
    if probes and snippets:
        msg = "局部对照不接收已验证片段"
        raise ValueError(msg)
    if (selected[0].sketch_id, selected[0].alternative) == (selected[1].sketch_id, selected[1].alternative):
        msg = "并行对照需要两个不同的方案选择"
        raise ValueError(msg)
    criteria = _shared_criteria(probes, criteria)
    package = read_planning_package(planning_dir)
    options = _planning_comparison_options(package, options, probes)
    if criteria is not None and (coverage := criteria.request.coverage) is not None:
        window = coverage.rendered_window
        if (window.right - window.left, window.bottom - window.top) != (options.width, options.height):
            msg = "评审冻结的 1:1 画布和对照的渲染尺寸须一致"
            raise ValueError(msg)
    directory = create_run_directory(options.output_dir)
    try:
        child_options = replace(options, output_dir=directory / "runs")
        prepared = tuple(
            _prepare_planning_generation(
                planning_dir,
                plan.sketch_id,
                alternative_index=plan.alternative,
                options=child_options,
                verified_snippets=snippets,
                local_probe=probes[index] if probes else None,
            )
            for index, plan in enumerate(selected)
        )
        models = (build_model(), build_model()) if model is None else (model, model)
        model_name = _validate_prepared_comparison(prepared, package, models)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    selected_review = replace(review_options or ReviewOptions(), output_dir=directory / "reviews")
    measurements = package.source.observation_manifest.reference_measurements

    def execute(side: int, budget: Context) -> PlanningComparisonItem:
        return budget.run(_run_planning_plan, selected[side], prepared[side], models[side], criteria, selected_review, measurements)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="shader-planning-comparison") as workers:
        first = workers.submit(execute, 0, copy_context())
        second = workers.submit(execute, 1, copy_context())
        items = first.result(), second.result()
    record = PlanningGenerationComparison(
        directory=str(directory),
        planning_sha256=package.content_sha256,
        reference_sha256=package.source.observation_manifest.reference.sha256,
        criteria_sha256=criteria.criteria_sha256 if criteria is not None else None,
        request=package.source.request,
        model_name=model_name,
        width=options.width,
        height=options.height,
        time=options.time,
        max_attempts=options.max_attempts,
        items=items,
    )
    write_planning_generation_comparison(record)
    return record
