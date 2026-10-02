"""从五库报告固定生成输入, 本阶段只准备材料, 不启动执行会话."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.generation_inputs import capture_generation_inputs

if TYPE_CHECKING:
    from collections.abc import Mapping

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
) -> PreparedGeneration:
    """内部准备入口; 未提供 options 时用原图宽高, 显式配置完整覆盖渲染条件."""
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
        prepared = PreparedGeneration(run_dir=directory, state=board, task_id=task_id, inputs=inputs, options=resolved, asset_root=root)
        _save_prepared(prepared)
    except BaseException:
        # 只清理由本次准备独占创建的目录, 输入错误或取消不会留下可误认作已执行的半份运行.
        shutil.rmtree(directory)
        raise
    return prepared
