"""将固定全图 M/S 方案接入现有生成、渲染与预览选择."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs, capture_planning_generation_inputs
from shader_deep.workflows.generation import _run_session

if TYPE_CHECKING:
    from shader_deep.agents.generation.contracts import GenerationOutcome
    from shader_deep.agents.generation.options import GenerationOptions
    from shader_deep.domain.tasks import BlackboardState


@dataclass(frozen=True, kw_only=True)
class PreparedPlanningGeneration:
    """新整图入口的固定材料, 生成循环仍由现有会话执行."""

    run_dir: Path
    state: BlackboardState
    inputs: CapturedPlanningInputs


def _planning_state(inputs: CapturedPlanningInputs) -> BlackboardState:
    target = TargetRecord(version="T1", request=inputs.request, reference_path=inputs.reference_path, constraints=("单 Pass GLSL ES 3.00 mainImage",))
    task = TaskRecord(
        id="G1",
        role="generation",
        target_version=target.version,
        objective=f"按固定整图方案 {inputs.binding.sketch_id} 实现完整 E/F/R",
        generation_binding=inputs.binding,
        allowed_changes=("实现生效 choices 与 composition, 保持跨元素关系", "依据原图调节方案内数值, 保留已知必要缺口"),
        stop_conditions=("选择已渲染并实际回读预览的候选, 如实记录差异及两类缺口",),
    )
    return add_task(add_target(new_blackboard(), target), task)


def _prepare_planning_generation(
    planning_dir: Path, sketch_id: str, *, alternative_index: int | None = None, options: GenerationOptions | None = None
) -> PreparedPlanningGeneration:
    directory = create_run_directory(options.output_dir if options is not None else None)
    try:
        inputs = capture_planning_generation_inputs(planning_dir, directory, sketch_id, alternative_index=alternative_index, options=options)
        state = _planning_state(inputs)
        save_run(directory, state, {"task_id": "G1", "phase": "prepared", "inputs": {"planning": inputs.metadata()}})
        return PreparedPlanningGeneration(run_dir=directory, state=state, inputs=inputs)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def run_generation_from_planning(
    planning_dir: Path, *, sketch_id: str, alternative_index: int | None = None, options: GenerationOptions | None = None
) -> GenerationOutcome:
    """从正式规划包显式选择整图方案并执行有预算的生成.

    Args:
        planning_dir: 自包含的 scene_planning_v1 报告目录.
        sketch_id: 明确采用的一套整图 S.
        alternative_index: 相对默认方案的一项零基局部备选.
        options: 完整执行条件和预算, 省略时继承规划尺寸与时间.

    Returns:
        实际候选、预览、来源及结束状态.

    Raises:
        ValueError: 正式包、选择、约束或模型配置无效.
        OSError: 输入捕获、输出或渲染材料无法访问.
    """
    prepared = _prepare_planning_generation(planning_dir, sketch_id, alternative_index=alternative_index, options=options)
    return _run_session(
        prepared.state,
        "G1",
        Path.cwd().resolve(),
        prepared.inputs.options,
        run_dir=prepared.run_dir,
        inputs=prepared.inputs,
    )
