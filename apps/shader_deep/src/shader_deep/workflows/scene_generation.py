"""固定两个已选元素, 由现有生成角色构造新的单 Pass 整图候选."""

from __future__ import annotations

import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.scene import ScenePlan
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.scene_inputs import capture_scene_inputs
from shader_deep.workflows.generation import _run_session

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shader_deep.agents.generation.contracts import GenerationOutcome
    from shader_deep.domain.scene import SceneElementSource
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.scene_inputs import CapturedSceneInputs


def _scene_state(inputs: CapturedSceneInputs) -> BlackboardState:
    plan = inputs.plan
    target = TargetRecord(
        version="T1",
        request=plan.request,
        reference_path=inputs.reference_path,
        constraints=("单 Pass GLSL ES 3.00 mainImage", f"背景要求: {plan.background}", f"布局与遮挡: {plan.layout}"),
    )
    # 源元素仅作为固定素材, 不把旧候选登记进本会话, 也不冒充单个报告绑定.
    task = TaskRecord(
        id="G1",
        role="generation",
        target_version=target.version,
        objective="整合两个已选元素, 产出完整整图并渲染自检",
        allowed_changes=("协调两个元素的函数、坐标、颜色和遮挡, 在自检中说明关键偏离",),
        stop_conditions=("选择本整图会话实际渲染并回读预览的候选, 或明确说明受阻原因",),
    )
    return add_task(add_target(new_blackboard(), target), task)


def run_scene_generation(
    elements: Sequence[SceneElementSource],
    request: str,
    *,
    background: str,
    layout: str = "保持原图布局与遮挡",
    width: int | None = None,
    height: int | None = None,
    time: float = 0.0,
    max_attempts: int = 3,
    output_dir: Path | None = None,
) -> GenerationOutcome:
    """将同一完整参考图的两个已选元素交给生成角色整合和渲染.

    !!! warning "实验性接口"
        首版只接收两个明确选定的单元素报告运行, 不自动挑选来源或拼接 shader 文件.

    Args:
        elements: 两个源运行与其实际已选候选; 完整原图从来源报告校验并固定.
        request: 本次整图要求.
        background: 整图背景要求.
        layout: 位置、布局与前后遮挡说明.
        width: 输出宽度, 未提供时使用固定原图宽度.
        height: 输出高度, 未提供时使用固定原图高度.
        time: 固定 iTime.
        max_attempts: 本次整图的渲染次数上限, 包含失败尝试.
        output_dir: 新整图运行目录的父目录.

    Returns:
        新整图会话的候选、停止原因及制品目录, 不改写元素源运行.

    Raises:
        ValueError: 来源不是已选候选、原图不一致、来源绑定已变化或参数无效.
        OSError: 来源或输出制品不可访问.
        Exception: 执行异常保留已有制品及目录诊断后原样抛出.
    """
    plan = ScenePlan(request=request, background=background, layout=layout, elements=tuple(elements))
    options = GenerationOptions(width=512 if width is None else width, height=512 if height is None else height, time=time, max_attempts=max_attempts)
    directory = create_run_directory(output_dir)
    try:
        inputs = capture_scene_inputs(plan, directory)
        state = _scene_state(inputs)
        options = replace(options, width=inputs.width if width is None else width, height=inputs.height if height is None else height)
    except BaseException:
        # 准备失败尚未执行模型; 只清理本次独占目录, 源运行始终保留.
        shutil.rmtree(directory)
        raise
    return _run_session(state, "G1", directory, options, run_dir=directory, inputs=inputs)
