"""固定五库报告方案并接入已有单 Agent 生成闭环."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.generation_inputs import capture_generation_inputs
from shader_deep.workflows.generation import _run_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shader_deep.agents.generation.contracts import GenerationOutcome
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.generation_inputs import CapturedGenerationInputs


@dataclass(frozen=True, kw_only=True)
class PreparedGeneration:
    """工作流内部持有的固定材料, 不是公共请求协议或模型检查点.

    Attributes:
        run_dir: 输入及后续候选共用的唯一运行目录.
        state: 已登记新目标与生成任务的黑板.
        task_id: 本次新任务标识.
        inputs: 已捕获的原图、方案及可选基线内容.
        options: 本轮固定渲染条件及预算.
        asset_root: 解析已有候选相对路径的根目录.
    """

    run_dir: Path
    state: BlackboardState
    task_id: str
    inputs: CapturedGenerationInputs
    options: GenerationOptions
    asset_root: Path


def _next_id(records: Mapping[str, object], prefix: str) -> str:
    number = 1
    while f"{prefix}{number}" in records:
        number += 1
    return f"{prefix}{number}"


def _register_task(state: BlackboardState, inputs: CapturedGenerationInputs, request: str, background: str) -> tuple[BlackboardState, str]:
    target_id, task_id = _next_id(state["targets"], "T"), _next_id(state["tasks"], "G")
    target = TargetRecord(
        version=target_id,
        request=request,
        reference_path=inputs.reference_path,
        constraints=("单 Pass GLSL ES 3.00 mainImage", f"背景要求: {background}"),
    )
    task = TaskRecord(
        id=task_id,
        role="generation",
        target_version=target_id,
        objective=f"按固定方案复现元素 {inputs.binding.element_id}, 保持其原画布位置",
        baseline_id=inputs.baseline.candidate_id if inputs.baseline is not None else None,
        generation_binding=inputs.binding,
        allowed_changes=("仅实现所选元素, 周围对象只作参考", "原图左上原点; GLSL fragCoord 使用左下原点"),
        stop_conditions=("选择实际渲染并回读预览的候选, 保留差异与未完成项",),
    )
    return add_task(add_target(state, target), task), task_id


def _save_prepared(prepared: PreparedGeneration) -> None:
    baseline = prepared.inputs.baseline
    # 文件映射随 run.json 保存, 源候选仍保持原路径, 不将内存中的代码或 data URL 重复写入快照.
    paths = (
        None if baseline is None else {"candidate_id": baseline.candidate_id, "code_path": baseline.code_path, "preview_path": baseline.preview_path}
    )
    save_run(
        prepared.run_dir,
        prepared.state,
        {
            "task_id": prepared.task_id,
            "asset_root": str(prepared.asset_root),
            "phase": "prepared",
            "inputs": {"baseline": paths},
            "render": {"width": prepared.options.width, "height": prepared.options.height, "time": prepared.options.time},
            "max_attempts": prepared.options.max_attempts,
        },
    )


def _prepare_generation(
    report_dir: Path,
    sketch_id: str,
    request: str,
    *,
    background: str,
    alternative: int | None = None,
    state: BlackboardState | None = None,
    baseline_id: str | None = None,
    asset_root: Path | None = None,
    options: GenerationOptions | None = None,
    size: tuple[int | None, int | None] | None = None,
) -> PreparedGeneration:
    """内部准备入口; options 保留完整配置语义, size 可逐项指定使用固定原图尺寸."""
    if not request.strip() or not background.strip():
        msg = "执行要求和背景要求不能为空"
        raise ValueError(msg)
    board = state if state is not None else new_blackboard()
    if baseline_id is not None and baseline_id not in board["candidates"]:
        msg = f"Unknown baseline candidate: {baseline_id}"
        raise ValueError(msg)
    root = (asset_root if asset_root is not None else Path.cwd()).resolve()
    directory = create_run_directory(options.output_dir if options is not None else None)
    try:
        baseline = board["candidates"][baseline_id] if baseline_id is not None else None
        inputs = capture_generation_inputs(report_dir, directory, sketch_id, alternative=alternative, baseline=baseline, asset_root=root)
        board, task_id = _register_task(board, inputs, request, background)
        # 显式 options 作为完整渲染配置, 不猜测调用方是否有意选择其中的默认尺寸.
        resolved = options or GenerationOptions(width=inputs.width, height=inputs.height)
        if size is not None:
            resolved = replace(resolved, width=inputs.width if size[0] is None else size[0], height=inputs.height if size[1] is None else size[1])
        prepared = PreparedGeneration(run_dir=directory, state=board, task_id=task_id, inputs=inputs, options=resolved, asset_root=root)
        _save_prepared(prepared)
    except BaseException:
        # 只清理由本次准备独占创建的目录, 输入错误或取消不会留下可误认作已执行的半份运行.
        shutil.rmtree(directory)
        raise
    return prepared


def run_generation_from_report(
    report_dir: Path,
    sketch_id: str,
    request: str,
    *,
    background: str,
    alternative: int | None = None,
    state: BlackboardState | None = None,
    baseline_id: str | None = None,
    asset_root: Path | None = None,
    width: int | None = None,
    height: int | None = None,
    time: float = 0.0,
    max_attempts: int = 3,
    output_dir: Path | None = None,
) -> GenerationOutcome:
    """固定明确选择的五库方案, 执行生成、渲染和预览自检.

    !!! warning "实验性接口"
        首版仅执行一个元素和一套显式方案, 不自动选择草图或切换备选.

    Args:
        report_dir: 完整五库报告目录.
        sketch_id: 本次明确采用的草图标识.
        request: 本次执行要求, 报告不能代替用户要求.
        background: 元素外背景的明确要求.
        alternative: 可选的一项局部备选索引.
        state: 需要沿用历史基线时提供的黑板.
        baseline_id: 已有黑板中明确指定的起始候选.
        asset_root: 已有候选相对制品路径的根目录.
        width: 显式渲染宽度, 未提供时使用捕获原图的宽度.
        height: 显式渲染高度, 未提供时使用捕获原图的高度.
        time: 本轮固定的 iTime.
        max_attempts: 含失败尝试的渲染次数上限.
        output_dir: 独立运行目录的父目录.

    Returns:
        含实际候选、唯一运行目录和结束原因的结果; 受阻或预算耗尽时没有选定候选.

    Raises:
        ValueError: 输入、选择或渲染配置无效.
        OSError: 输入或输出制品不可访问.
        Exception: 执行异常原样抛出, 已有目录和错误诊断保存在运行快照及异常注释中.
    """
    options = GenerationOptions(
        width=512 if width is None else width,
        height=512 if height is None else height,
        time=time,
        max_attempts=max_attempts,
        output_dir=output_dir,
    )
    prepared = _prepare_generation(
        report_dir,
        sketch_id,
        request,
        background=background,
        alternative=alternative,
        state=state,
        baseline_id=baseline_id,
        asset_root=asset_root,
        options=options,
        size=(width, height),
    )
    return _run_session(prepared.state, prepared.task_id, prepared.asset_root, prepared.options, run_dir=prepared.run_dir, inputs=prepared.inputs)
