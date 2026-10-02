"""同一固定输入下顺序执行两套方案, 保留独立结果供人工比较."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import new_blackboard
from shader_deep.domain.generation_comparison import ComparisonItem, GenerationComparison, GenerationPlan, validate_generation_plans
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.generation_comparison import write_generation_comparison
from shader_deep.infrastructure.storage.generation_inputs import _reuse_generation_inputs
from shader_deep.workflows.generation import _run_session
from shader_deep.workflows.generation_from_report import PreparedGeneration, _prepare_generation, _register_task, _save_prepared

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.domain.tasks import BlackboardState


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
