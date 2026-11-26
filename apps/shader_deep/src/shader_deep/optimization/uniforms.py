"""将参数合同、WebGL2 回读和受保护的数值搜索组合起来."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from shader_deep.domain.shader_parameters import bake_uniforms, resolve_uniforms
from shader_deep.optimization.objective import display_samples, prepare_objective
from shader_deep.optimization.search import _minimize

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractContextManager

    from shader_deep.domain.shader_parameters import ParameterSpec, UniformInput, UniformValue
    from shader_deep.domain.visual_review import PixelRegion
    from shader_deep.optimization.objective import ProtectedRegion, RegionBox, _Objective
    from shader_deep.optimization.search import Point

DEFAULT_OPTIMIZATION_EVALUATIONS = 48
MAX_OPTIMIZATION_EVALUATIONS = 128
_MAX_DIMENSIONS = 20
_MIN_GAIN = 0.0001
_MAX_TURNING_POINTS = 2
_BOUNDARY_TOLERANCE = 1.0 / 4096
OptimizationStatus = Literal["improved", "baseline_satisfied", "structure_bottleneck"]


class _Session(Protocol):
    def read_regions(self, uniforms: Mapping[str, UniformValue], regions: Sequence[RegionBox]) -> tuple[bytes, ...]: ...

    def render(self, uniforms: Mapping[str, UniformValue]) -> bytes: ...


class _Renderer(Protocol):
    def optimization_session(
        self, glsl_code: str, width: int, height: int, time: float, *, uniforms: Mapping[str, UniformValue]
    ) -> AbstractContextManager[_Session]: ...


@dataclass(frozen=True)
class TurningPoint:
    """达到新最低误差时的一次实测参数和正式预览."""

    evaluation: int
    error: float
    uniforms: dict[str, UniformValue]
    png: bytes


@dataclass(frozen=True)
class OptimizationResult:
    """记录保留参数、误差、预算用量与正式 PNG, 供生成工具或控制面消费.

    Attributes:
        status: 找到有效改善、起始误差低于最小有效下降幅度, 或遇到结构瓶颈.
        uniforms: 改善时为胜出参数, 否则为起始参数.
        start_error: 起始误差, 以 0-1 的归一化目标度量.
        end_error: 返回参数的目标误差.
        best_observed_error: 保护区检查通过的最小试验误差.
        evaluations: 真实像素试验数, 包括起始评估.
        boundary_parameters: 最低误差试验中逼近硬边界的参数名.
        rejected_trials: 因保护区漂移被拒的试验数.
        baseline_render: 起始参数组的完整 PNG.
        best_render: 改善时导出的完整胜出 PNG, 其他情形为空.
        turning_points: 保护区通过且误差显著下降的中途候选, 最多两项.
    """

    status: OptimizationStatus
    uniforms: dict[str, UniformValue]
    start_error: float
    end_error: float
    best_observed_error: float
    evaluations: int
    boundary_parameters: tuple[str, ...]
    rejected_trials: int
    baseline_render: bytes
    best_render: bytes | None
    turning_points: tuple[TurningPoint, ...] = ()


def _vector(value: UniformValue) -> tuple[float, ...]:
    return value if isinstance(value, tuple) else (value,)


@dataclass(frozen=True)
class _Component:
    name: str
    index: int
    low: float
    high: float


@dataclass(frozen=True)
class _Parameters:
    baseline: dict[str, UniformValue]
    components: tuple[_Component, ...]
    origin: Point

    def expand(self, point: Point) -> dict[str, UniformValue]:
        if point == self.origin:
            return self.baseline
        values = {name: list(_vector(value)) for name, value in self.baseline.items()}
        for x, part in zip(point, self.components, strict=True):
            # 对极不对称的浮点边界, 插值后仍强制裁入原合同范围.
            values[part.name][part.index] = max(part.low, min(part.high, part.low * (1 - x) + part.high * x))
        return {name: tuple(value) if isinstance(self.baseline[name], tuple) else value[0] for name, value in values.items()}

    def at_boundary(self, point: Point) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(part.name for value, part in zip(point, self.components, strict=True) if min(value, 1 - value) <= _BOUNDARY_TOLERANCE)
        )


def _components(specs: Sequence[ParameterSpec], names: set[str]) -> tuple[_Component, ...]:
    return tuple(
        _Component(spec.name, index, low, high)
        for spec in specs
        if spec.name in names
        for index, (low, high) in enumerate(zip(_vector(spec.min), _vector(spec.max), strict=True))
        if low < high
    )


def _parameters(specs: Sequence[ParameterSpec], uniforms: Mapping[str, UniformInput] | None, parameter_names: Sequence[str] | None) -> _Parameters:
    baseline = resolve_uniforms(specs, uniforms)
    names = set(baseline if parameter_names is None else parameter_names)
    if names - baseline.keys():
        msg = "Optimization may only tune declared uniform names"
        raise ValueError(msg)
    components = _components(specs, names)
    if not 1 <= len(components) <= _MAX_DIMENSIONS:
        msg = f"Select between 1 and {_MAX_DIMENSIONS} adjustable uniform components for each optimization"
        raise ValueError(msg)
    origin = tuple((_vector(baseline[part.name])[part.index] - part.low) / (part.high - part.low) for part in components)
    return _Parameters(baseline, components, origin)


def _turning_points(
    session: _Session, parameters: _Parameters, cache: Mapping[Point, float], winner: Point, start: float
) -> tuple[TurningPoint, ...]:
    # 按评估顺序追踪历史新低, 在结束后仅为下降幅度最大的中间步骤重新画整图.
    improvements = []
    lowest = start
    for evaluation, (point, loss) in enumerate(cache.items(), start=1):
        if loss < lowest:
            gain = lowest - loss
            if point != winner and gain >= _MIN_GAIN:
                improvements.append((gain, evaluation, point, loss))
            lowest = loss
    selected = sorted(improvements, key=lambda item: -item[0])[:_MAX_TURNING_POINTS]
    result = []
    for _, evaluation, point, loss in sorted(selected, key=lambda item: item[1]):
        uniforms = parameters.expand(point)
        result.append(TurningPoint(evaluation, loss, uniforms, session.render(uniforms)))
    return tuple(result)


def _run(session: _Session, objective: _Objective, parameters: _Parameters, budget: int) -> OptimizationResult:
    baseline = parameters.baseline
    rejected = 0
    with display_samples(objective.boxes, session.read_regions(baseline, objective.boxes)) as initial:
        start = objective.error(initial)
        if start < _MIN_GAIN:
            return OptimizationResult(
                status="baseline_satisfied",
                uniforms=baseline,
                start_error=start,
                end_error=start,
                best_observed_error=start,
                evaluations=1,
                boundary_parameters=parameters.at_boundary(parameters.origin),
                rejected_trials=0,
                baseline_render=session.render(baseline),
                best_render=None,
            )

        def score(point: Point) -> float:
            nonlocal rejected
            if point == parameters.origin:
                return start
            pixels = session.read_regions(parameters.expand(point), objective.boxes)
            with display_samples(objective.boxes, pixels) as candidate:
                if objective.regresses(initial, candidate):
                    rejected += 1
                    return math.inf
                return objective.error(candidate)

        search = _minimize(score, parameters.origin, budget)
    improved = start - search.error >= _MIN_GAIN
    best = parameters.expand(search.best) if improved else baseline
    return OptimizationResult(
        status="improved" if improved else "structure_bottleneck",
        uniforms=best,
        start_error=start,
        end_error=search.error if improved else start,
        best_observed_error=search.error,
        evaluations=len(search.cache),
        boundary_parameters=parameters.at_boundary(search.best),
        rejected_trials=rejected,
        baseline_render=session.render(baseline),
        best_render=session.render(best) if improved else None,
        turning_points=_turning_points(session, parameters, search.cache, search.best, start) if improved else (),
    )


def optimize_uniforms(
    renderer: _Renderer,
    glsl_code: str,
    reference_png: bytes,
    width: int,
    height: int,
    time: float,
    *,
    specs: Sequence[ParameterSpec],
    uniforms: Mapping[str, UniformInput] | None = None,
    parameter_names: Sequence[str] | None = None,
    regions: Sequence[PixelRegion] = (),
    anchors: Sequence[PixelRegion] = (),
    protected_regions: Sequence[ProtectedRegion] = (),
    max_evaluations: int = DEFAULT_OPTIMIZATION_EVALUATIONS,
) -> OptimizationResult:
    """保持代码、画布、时间与非白名单参数固定, 搜索低误差的参数组.

    Args:
        renderer: 已启动的 WebGL2 渲染器.
        glsl_code: 已确定结构的单 Pass GLSL.
        reference_png: 与渲染画布 1:1 对齐的参考 PNG.
        width: 固定画布宽度, 单位为像素.
        height: 固定画布高度, 单位为像素.
        time: 固定渲染时间, 单位为秒.
        specs: GLSL 中所有外置参数的完整合同.
        uniforms: 起始赋值, 省略项使用声明的默认值.
        parameter_names: 可调参数的可选白名单, 默认使用所有参数.
        regions: 优化颜色和边缘的目标区, 为空时使用整幅图像.
        anchors: 用于对齐绝对和相对统计的可选区域.
        protected_regions: 以起始候选为基准的逐像素漂移硬约束.
        max_evaluations: 试验像素回读预算, 包括起始评估.

    Returns:
        参数、误差、保护区诊断与完整候选 PNG.

    Raises:
        ValueError: 合同、白名单、输入尺寸、区域或预算无效.
        RenderError: 编译、链接、uniform 检查或回读失败.
    """
    if isinstance(max_evaluations, bool) or not isinstance(max_evaluations, int) or not 1 <= max_evaluations <= MAX_OPTIMIZATION_EVALUATIONS:
        msg = f"Optimization budget must be an integer from 1 to {MAX_OPTIMIZATION_EVALUATIONS}"
        raise ValueError(msg)
    parameters = _parameters(specs, uniforms, parameter_names)
    # 先校验固定 GLSL 可烘焙, 但评估始终使用未经修改的参数化基线.
    bake_uniforms(glsl_code, specs, parameters.baseline)
    with (
        prepare_objective(reference_png, width, height, regions, anchors, protected_regions) as objective,
        renderer.optimization_session(glsl_code, width, height, time, uniforms=parameters.baseline) as session,
    ):
        return _run(session, objective, parameters, max_evaluations)
