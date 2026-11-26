"""固定正式整图规划与显式选择, 供生成请求逐轮复用."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.generation import ScenePlanningBinding
from shader_deep.domain.local_probe import probe_dependency_digest, probe_mechanism_ids
from shader_deep.domain.scene_observations import REFERENCE_MEASUREMENT_LIMIT
from shader_deep.domain.scene_planning import effective_planning_sketch
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.storage.parameterized_candidate import read_candidate_parameters
from shader_deep.infrastructure.storage.planning_inputs import GenerationConstraints, package_asset
from shader_deep.infrastructure.storage.planning_package import PLANNING_FILES, PlanningPackage, read_planning_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shader_deep.domain.local_probe import LocalProbeBinding, VerifiedSnippet
    from shader_deep.domain.scene_planning import EffectivePlanningSketch
    from shader_deep.domain.tasks import CandidateRecord


@dataclass(frozen=True, kw_only=True)
class CapturedSnippet:
    """一份通过局部评审且已复制到本轮目录的生成材料."""

    code: str
    code_path: str
    preview_path: str
    metadata_path: str
    metadata_json: str


@dataclass(frozen=True, kw_only=True)
class CapturedRevision:
    """由本轮复制的旧候选和已批准修订靶点."""

    guidance: str
    code: str | None = None
    preview_path: str | None = None
    parameterized_render: str | None = None
    files: tuple[str, ...] = ()


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
    reference_anchors: tuple[PixelRegion, ...] = ()
    verified_snippets: tuple[CapturedSnippet, ...] = ()
    local_probe: LocalProbeBinding | None = None
    revision: CapturedRevision | None = None

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
            "verified_snippets": [item.metadata_path for item in self.verified_snippets],
            "local_probe": scheme.get("local_probe"),
            "revision_inputs": list(self.revision.files) if self.revision is not None else [],
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


def _scheme(package: PlanningPackage, selected: EffectivePlanningSketch, options: GenerationOptions, probe: LocalProbeBinding | None) -> str:
    source = package.source
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
    if probe is not None:
        payload["local_probe"] = probe.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _dependency_digest(package: PlanningPackage, chosen: EffectivePlanningSketch, probe: LocalProbeBinding) -> str:
    source = package.source
    return probe_dependency_digest(
        source.observations, chosen, probe.target_ids, reference_sha256=source.observation_manifest.reference.sha256, request=source.request
    )


def bind_probe_dependencies(probe: LocalProbeBinding, package: PlanningPackage) -> LocalProbeBinding:
    """从校验过的原规划包补齐旧试验绑定的依赖身份."""
    if probe.planning_sha256 != package.content_sha256:
        msg = "局部试验与原规划包指纹不匹配"
        raise ValueError(msg)
    source = package.source
    original = effective_planning_sketch(
        package.report,
        source.observations,
        probe.sketch_id,
        alternative_index=probe.alternative_index,
        upstream_gaps=source.observation_manifest.gaps,
    )
    if probe.mechanism_ids != probe_mechanism_ids(source.observations, original, probe.target_ids):
        msg = "局部试验与原规划选定的方法不匹配"
        raise ValueError(msg)
    return probe.model_copy(update={"dependency_sha256": _dependency_digest(package, original, probe)})


def can_reuse_probe(probe: LocalProbeBinding, package: PlanningPackage, chosen: EffectivePlanningSketch, canvas: tuple[int, int, float]) -> bool:
    """核对局部评审的参考图、目标依赖、方法内容和实际渲染条件."""
    if canvas != (probe.width, probe.height, probe.time) or not set(probe.target_ids) <= package.source.observations.identities:
        return False
    if probe.context_mode == "scene_baseline_patch" and (
        probe.planning_sha256 != package.content_sha256 or probe.sketch_id != chosen.sketch_id or probe.alternative_index != chosen.alternative_index
    ):
        # 片段是完整的旧场景着色器, 组合次序及其余对象都参与了这次局部评审.
        return False
    if set(probe.mechanism_ids) != set(probe_mechanism_ids(package.source.observations, chosen, probe.target_ids)):
        return False
    if probe.dependency_sha256 is None:
        if probe.planning_sha256 != package.content_sha256:
            return False
        probe = bind_probe_dependencies(probe, package)
    return probe.dependency_sha256 == _dependency_digest(package, chosen, probe)


def _capture_snippets(
    snippets: Sequence[VerifiedSnippet], directory: Path, package: PlanningPackage, chosen: EffectivePlanningSketch, options: GenerationOptions
) -> tuple[CapturedSnippet, ...]:
    result: list[CapturedSnippet] = []
    canvas = (options.width, options.height, options.time)
    for number, snippet in enumerate(snippets, start=1):
        if not can_reuse_probe(snippet.binding, package, chosen, canvas):
            msg = "局部片段的参考图、目标方法依赖或渲染条件与本次整图不匹配"
            raise ValueError(msg)
        path = directory / f"{number:03d}"
        path.mkdir(parents=True)
        metadata = snippet.metadata()
        code, preview, record = path / "snippet.glsl", path / "preview.png", path / "snippet.json"
        code.write_text(snippet.glsl_code, encoding="utf-8")
        preview.write_bytes(snippet.preview_png)
        write_snapshot(record, metadata)
        result.append(
            CapturedSnippet(
                code=snippet.glsl_code,
                code_path=str(code),
                preview_path=str(preview),
                metadata_path=str(record),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        )
    return tuple(result)


def _anchors(package: PlanningPackage, options: GenerationOptions) -> tuple[PixelRegion, ...]:
    # 观察包可继承旧测量, 自动优化仍为目标与保护区域预留回读位置.
    measurements = package.source.observation_manifest.reference_measurements
    return tuple(
        PixelRegion(left=item.region.left, top=item.region.top, right=item.region.right, bottom=item.region.bottom)
        for item in measurements
        if item.kind == "region_stats" and item.region.right <= options.width and item.region.bottom <= options.height
    )[-REFERENCE_MEASUREMENT_LIMIT:]


def _capture_revision(candidate: CandidateRecord | None, guidance: str | None, directory: Path) -> CapturedRevision | None:
    if candidate is None:
        return CapturedRevision(guidance=guidance) if guidance else None
    if candidate.preview_path is None:
        msg = "修订基线需要成功渲染的预览"
        raise ValueError(msg)
    directory.mkdir(parents=True)
    code, preview = Path(candidate.code_path), Path(candidate.preview_path)
    parameter_record = Path(candidate.render_record_path) if candidate.render_record_path is not None else None
    if parameter_record is not None:
        read_candidate_parameters(parameter_record, code, preview)
    source, target_png = code.read_text(encoding="utf-8"), directory / "preview.png"
    target_code = directory / "baseline.glsl"
    target_code.write_text(source, encoding="utf-8")
    target_png.write_bytes(preview.read_bytes())
    files = [str(target_code), str(target_png)]
    settings = None
    if parameter_record is not None:
        settings = parameter_record.read_text(encoding="utf-8")
        target = directory / "render.json"
        target.write_text(settings, encoding="utf-8")
        files.append(str(target))
    return CapturedRevision(guidance=guidance or "", code=source, preview_path=str(target_png), parameterized_render=settings, files=tuple(files))


def _binding_matches(probe: LocalProbeBinding, package: PlanningPackage, sketch_id: str, alternative_index: int | None) -> bool:
    observations = package.source.observations
    if (
        probe.planning_sha256 != package.content_sha256
        or probe.sketch_id != sketch_id
        or probe.alternative_index != alternative_index
        or not set(probe.target_ids) <= observations.identities
    ):
        return False
    chosen = effective_planning_sketch(
        package.report, observations, sketch_id, alternative_index=alternative_index, upstream_gaps=package.source.observation_manifest.gaps
    )
    return probe.mechanism_ids == probe_mechanism_ids(observations, chosen, probe.target_ids) and (
        probe.dependency_sha256 is None or probe.dependency_sha256 == _dependency_digest(package, chosen, probe)
    )


def capture_planning_generation_inputs(
    planning_dir: Path,
    run_dir: Path,
    sketch_id: str,
    *,
    alternative_index: int | None = None,
    options: GenerationOptions | None = None,
    verified_snippets: Sequence[VerifiedSnippet] = (),
    local_probe: LocalProbeBinding | None = None,
    revision_candidate: CandidateRecord | None = None,
    revision_guidance: str | None = None,
) -> CapturedPlanningInputs:
    """捕获规划包并明确本轮采用的整图方案和执行条件.

    Args:
        planning_dir: 已发布的 scene_planning_v1 包.
        run_dir: 新生成运行目录.
        sketch_id: 显式选择的一套整图方案.
        alternative_index: 可选的一项零基局部备选.
        options: 完整执行配置, 省略时继承规划尺寸与时间.
        verified_snippets: 局部通过的固定 GLSL、参数、预览与评审来源.
        local_probe: 局部运行的固定上下文与小预算.
        revision_candidate: 已有候选的固定源码和预览, 作为本轮修订参考.
        revision_guidance: 已批准的修订靶点及保护要求.

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
    if local_probe is not None:
        if verified_snippets or not _binding_matches(local_probe, package, sketch_id, alternative_index):
            msg = "局部绑定必须属于当前固定规划, 已验证片段仅供整图组合"
            raise ValueError(msg)
        actual = options or GenerationOptions(
            width=local_probe.width, height=local_probe.height, time=local_probe.time, max_attempts=local_probe.max_attempts, optimize_parameters=True
        )
        if (actual.width, actual.height, actual.time) != (
            local_probe.width,
            local_probe.height,
            local_probe.time,
        ) or actual.max_attempts > local_probe.max_attempts:
            msg = "局部渲染条件与尝试预算必须遵守已冻结的试验绑定"
            raise ValueError(msg)
    else:
        actual = options or GenerationOptions(width=constraints.width, height=constraints.height, time=constraints.time, optimize_parameters=True)
    selected = effective_planning_sketch(
        package.report, source.observations, sketch_id, alternative_index=alternative_index, upstream_gaps=source.observation_manifest.gaps
    )
    snippets = _capture_snippets(verified_snippets, destination.parent / "snippets", package, selected, actual)
    revision = _capture_revision(revision_candidate, revision_guidance, destination.parent / "revision")
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
        scheme_json=_scheme(package, selected, actual, local_probe),
        files=(
            *(str(destination / name) for name in PLANNING_FILES),
            *(path for item in snippets for path in (item.code_path, item.preview_path, item.metadata_path)),
            *(revision.files if revision is not None else ()),
        ),
        planned_constraints=constraints,
        options=actual,
        reference_anchors=_anchors(package, actual),
        verified_snippets=snippets,
        local_probe=local_probe,
        revision=revision,
    )
