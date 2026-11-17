"""固定正式整图规划与显式选择, 供生成请求逐轮复用."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.generation import ScenePlanningBinding
from shader_deep.domain.scene_planning import effective_planning_sketch
from shader_deep.infrastructure.storage.planning_inputs import GenerationConstraints, package_asset
from shader_deep.infrastructure.storage.planning_package import PLANNING_FILES, PlanningPackage, read_planning_package


@dataclass(frozen=True, kw_only=True)
class CapturedPlanningInputs:
    """本轮整图任务的方案正文、图像字节与条件身份."""

    binding: ScenePlanningBinding
    request: str
    reference_path: str
    reference_url: str
    scheme_json: str
    files: tuple[str, ...]
    planned_constraints: GenerationConstraints
    options: GenerationOptions

    @property
    def limitations(self) -> tuple[str, ...]:
        """返回继承的必要缺口, 供实际候选选择结果保留."""
        selected = json.loads(self.scheme_json)["selected"]
        return tuple(
            f"{label} ({', '.join(gap['refs']) or '全图'}): {gap['description']}"
            for name, label in (("observation_gaps", "观察缺口"), ("gaps", "规划缺口"))
            for gap in selected[name]
        )

    def metadata(self) -> dict[str, object]:
        """返回落盘的来源与执行差异, 图像和正文从捕获包读取."""
        scheme = json.loads(self.scheme_json)
        return {
            "report_path": self.binding.report_path,
            "content_sha256": self.binding.content_sha256,
            "observation_sha256": self.binding.observation_sha256,
            "sketch_id": self.binding.sketch_id,
            "alternative_index": self.binding.alternative,
            "planned_constraints": self.planned_constraints.model_dump(mode="json"),
            "execution_render": scheme["execution_render"],
            "render_overrides": scheme["render_overrides"],
            "observation_gaps": scheme["selected"]["observation_gaps"],
            "planning_gaps": scheme["selected"]["gaps"],
            "complete": scheme["selected"]["complete"],
        }


def _capture_package(source: Path, destination: Path) -> PlanningPackage:
    original = read_planning_package(source)
    contents = {name: package_asset(source, name).read_bytes() for name in PLANNING_FILES}
    destination.mkdir(parents=True)
    for name, content in contents.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    captured = read_planning_package(destination)
    if captured.content_sha256 != original.content_sha256:
        msg = "规划包在生成捕获期间发生变化"
        raise ValueError(msg)
    return captured


def _scheme(package: PlanningPackage, sketch_id: str, alternative_index: int | None, options: GenerationOptions) -> str:
    source = package.source
    selected = effective_planning_sketch(
        package.report, source.observations, sketch_id, alternative_index=alternative_index, upstream_gaps=source.observation_manifest.gaps
    )
    actual = {"width": options.width, "height": options.height, "time": options.time}
    planned = source.constraints.model_dump(mode="json")
    payload = {
        "schema_version": "scene_planning_v1",
        "content_sha256": package.content_sha256,
        "observation_sha256": source.observation_sha256,
        "observations": source.observations.model_dump(mode="json"),
        "context_notes": source.observation_manifest.context_notes,
        "open_questions": [item.model_dump(mode="json") for item in source.observation_manifest.open_questions],
        "selected": selected.model_dump(mode="json"),
        "planned_constraints": planned,
        "execution_render": actual,
        "render_overrides": {name: {"planned": planned[name], "actual": value} for name, value in actual.items() if planned[name] != value},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def capture_planning_generation_inputs(
    planning_dir: Path,
    run_dir: Path,
    sketch_id: str,
    *,
    alternative_index: int | None = None,
    options: GenerationOptions | None = None,
) -> CapturedPlanningInputs:
    """捕获规划包并明确本轮采用的整图方案和执行条件.

    Args:
        planning_dir: 已发布的 scene_planning_v1 包.
        run_dir: 新生成运行目录.
        sketch_id: 显式选择的一套整图方案.
        alternative_index: 可选的一项零基局部备选.
        options: 完整执行配置, 省略时继承规划尺寸与时间.

    Returns:
        正式副本、实际图像内容与已固定的生效方案.

    Raises:
        ValueError: 包、选择或生成约束无效.
        OSError: 固定材料无法读取或写入.
    """
    destination = (run_dir / "inputs" / "planning").resolve()
    package = _capture_package(Path(planning_dir), destination)
    source = package.source
    constraints = source.constraints
    actual = options or GenerationOptions(width=constraints.width, height=constraints.height, time=constraints.time)
    return CapturedPlanningInputs(
        binding=ScenePlanningBinding(
            report_path=str(destination),
            content_sha256=package.content_sha256,
            observation_sha256=source.observation_sha256,
            reference_sha256=source.observation_manifest.reference.sha256,
            sketch_id=sketch_id,
            alternative=alternative_index,
        ),
        request=source.request,
        reference_path=str(source.reference_path),
        reference_url=source.reference_url,
        scheme_json=_scheme(package, sketch_id, alternative_index, actual),
        files=tuple(str(destination / name) for name in PLANNING_FILES),
        planned_constraints=constraints,
        options=actual,
    )
