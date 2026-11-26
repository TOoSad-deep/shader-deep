"""绑定局部试验并仅回流仍满足固定目标的已验证片段."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.local_probe import VerifiedSnippet, probe_observation_dependencies
from shader_deep.domain.scene_planning import effective_planning_sketch
from shader_deep.domain.visual_review import BoundReviewCriteria, PixelRegion
from shader_deep.infrastructure.storage.generation_planning_inputs import bind_probe_dependencies, can_reuse_probe
from shader_deep.infrastructure.storage.parameterized_candidate import read_candidate_parameters
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.workflows.optimization_workflow.candidates import _material, _require_review
from shader_deep.workflows.optimization_workflow.contracts import VerifiedProbe
from shader_deep.workflows.optimization_workflow.stages import _read_frozen
from shader_deep.workflows.planning.review_criteria import NoLocalReviewChecksError, bind_local_probe, derive_local_review_request
from shader_deep.workflows.review import freeze_workflow_review

if TYPE_CHECKING:
    from shader_deep.domain.generation_comparison import GenerationPlan
    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.scene_planning import EffectivePlanningSketch, RiskProbe
    from shader_deep.infrastructure.storage.planning_package import PlanningPackage
    from shader_deep.workflows.optimization_workflow.contracts import GateADecision, WorkflowCandidate, _ProbeDecision
    from shader_deep.workflows.optimization_workflow.stages import _Context


def _probe(
    context: _Context,
    choice: _ProbeDecision,
    package: PlanningPackage,
    criteria: BoundReviewCriteria,
    sketch: str,
    alternative: int | None,
    *,
    override: RiskProbe | None = None,
) -> LocalProbeBinding | None:
    if choice.probe_id is None and override is None:
        return None
    if choice.target_region is None:
        msg = "Local probe selection requires a target region"
        raise ValueError(msg)
    selected = override if override is not None else _planned_probe(choice.probe_id, package, sketch, alternative)
    return _bind(context, choice, package, criteria, sketch, alternative, selected)


def _planned_probe(index: int | None, package: PlanningPackage, sketch: str, alternative: int | None) -> RiskProbe:
    active = effective_planning_sketch(package.report, package.source.observations, sketch, alternative_index=alternative)
    if index is None or index >= len(active.risk_probes):
        msg = "Select a valid zero-based probe ID and its region"
        raise ValueError(msg)
    return active.risk_probes[index]


def _gate_probes(
    context: _Context,
    choice: GateADecision,
    package: PlanningPackage,
    criteria: BoundReviewCriteria,
    sketch: str,
) -> tuple[LocalProbeBinding, ...]:
    alternative = choice.alternative_index
    if choice.action == "run_probe":
        return tuple(
            _bind(context, selection, package, criteria, sketch, alternative, _planned_probe(selection.probe_id, package, sketch, alternative))
            for selection in choice.probes or (choice,)
        )
    rival = choice.comparison_plan
    if choice.action == "compare_plans" and rival is not None:
        effective_planning_sketch(package.report, package.source.observations, rival.sketch_id, alternative_index=rival.alternative_index)
        if choice.probe_id is not None:
            risk = _planned_probe(choice.probe_id, package, sketch, alternative)
            return tuple(
                _bind(context, choice, package, criteria, name, index, risk)
                for name, index in ((sketch, alternative), (rival.sketch_id, rival.alternative_index))
            )
    return ()


def _bind(
    context: _Context,
    choice: _ProbeDecision,
    package: PlanningPackage,
    criteria: BoundReviewCriteria,
    sketch: str,
    alternative: int | None,
    selected: RiskProbe,
) -> LocalProbeBinding:
    if choice.target_region is None:
        msg = "Local probe selection requires a target region"
        raise ValueError(msg)
    baseline = context.state.current_candidate
    code = None
    if selected.context_mode == "scene_baseline_patch":
        if baseline is None or baseline.scope != "scene":
            msg = "Patch probing requires a fixed scene candidate"
            raise ValueError(msg)
        material = _material(context, baseline)
        code = (context.asset(baseline.baked_code_path) if baseline.baked_code_path is not None else Path(material.code_path)).read_text(
            encoding="utf-8"
        )
    color = choice.solid_rgba
    if color is None and selected.context_mode == "isolated_solid":
        color = (0.25, 0.25, 0.25, 1.0)
    binding = bind_local_probe(
        package,
        sketch,
        selected,
        criteria,
        choice.target_region,
        alternative_index=alternative,
        solid_rgba=color,
        proxy_context=choice.proxy_context,
        baseline_glsl=code,
        patch_source=choice.patch_source,
    )
    if baseline is not None and (context.state.phase == "gate_b" or selected.context_mode == "scene_baseline_patch"):
        return binding.model_copy(update={"time": baseline.time})
    return binding


def _require_probe_frame(context: _Context, candidate: WorkflowCandidate) -> None:
    report = _require_review(context, candidate)
    if report.source.request.conditions.rendered_time != candidate.time:
        msg = "局部片段实际渲染时间与评审包不匹配"
        raise ValueError(msg)


def _remember_probe(context: _Context, candidate: WorkflowCandidate) -> None:
    probe = candidate.probe
    if probe is None or candidate.review_sha256 is None or candidate.verdict != "no_major_issue_observed" or candidate.regressions:
        msg = "Only a passing, reviewed local candidate can be promoted"
        raise ValueError(msg)
    _require_probe_frame(context, candidate)
    _, observations = context.observation()
    record = VerifiedProbe(candidate_id=candidate.id, dependencies=probe_observation_dependencies(observations, probe.target_ids))
    retained = tuple(item for item in context.state.verified_probes if item.candidate_id != candidate.id)
    context.update(verified_probes=(*retained, record))


def _snippet(context: _Context, candidate: WorkflowCandidate, binding: LocalProbeBinding) -> VerifiedSnippet:
    material = _material(context, candidate)
    _require_probe_frame(context, candidate)
    code, preview = context.asset(candidate.code_path), context.asset(candidate.preview_path)
    parameters = read_candidate_parameters(Path(material.render_record_path), code, preview) if material.render_record_path else None
    if candidate.review_sha256 is None:
        msg = "Verified probe is missing its review fingerprint"
        raise ValueError(msg)
    return VerifiedSnippet(
        binding=binding,
        glsl_code=code.read_text(encoding="utf-8"),
        uniforms=parameters.uniforms if parameters is not None else {},
        preview_png=preview.read_bytes(),
        review_criteria_sha256=binding.criteria.criteria_sha256,
        review_report_sha256=candidate.review_sha256,
    )


def _local_standard(criteria: BoundReviewCriteria, scene: BoundReviewCriteria) -> dict[str, object]:
    """比较实际局部标准, 去掉仅用于提示模型无关目标的检查文本."""
    dimensions = {row.id: row for row in scene.request.dimensions}
    normalized = []
    for row in criteria.request.dimensions:
        indirect = set(dimensions[row.id].required_checks) - set(row.required_checks)
        exclusions = tuple(text for text in row.excluded_checks if text not in indirect) if row.required_checks else ()
        normalized.append(row.model_copy(update={"excluded_checks": exclusions}))
    request = criteria.request.model_copy(update={"dimensions": tuple(normalized)})
    return {**criteria.model_dump(exclude={"criteria_sha256", "request"}), "request": request}


def _same_local_standard(
    context: _Context, candidate: WorkflowCandidate, previous: BoundReviewCriteria, updated: BoundReviewCriteria, scene: BoundReviewCriteria
) -> bool:
    if updated.criteria_sha256 == previous.criteria_sha256:
        return True
    old = BoundReviewCriteria.model_validate_json(context.asset(candidate.scene_criteria_path).read_bytes())
    if freeze_workflow_review(old.request).criteria_sha256 != candidate.scene_criteria_sha256:
        return False
    return _local_standard(previous, old) == _local_standard(updated, scene)


def _snippet_choices(
    package: PlanningPackage, sketch_id: str | None, alternative_index: int | None, compared_with: GenerationPlan | None
) -> tuple[EffectivePlanningSketch, ...]:
    if sketch_id is None:
        msg = "复用局部片段前须选择整图方案"
        raise ValueError(msg)
    selections = [(sketch_id, alternative_index)]
    if compared_with is not None:
        selections.append((compared_with.sketch_id, compared_with.alternative))
    return tuple(
        effective_planning_sketch(
            package.report, package.source.observations, sketch, alternative_index=alternative, upstream_gaps=package.source.observation_manifest.gaps
        )
        for sketch, alternative in selections
    )


def _snippets(context: _Context, time: float, *, compared_with: GenerationPlan | None = None) -> tuple[VerifiedSnippet, ...]:
    if not context.state.verified_probes:
        return ()
    scene = _read_frozen(context)
    state = context.state
    package = read_planning_package(context.asset(state.planning_path))
    observations = package.source.observations
    chosen = _snippet_choices(package, state.sketch_id, state.alternative_index, compared_with)
    canvas = PixelRegion(left=0, top=0, right=state.width, bottom=state.height)
    candidates = {item.id: item for item in context.state.candidates}
    retained = []
    for record in context.state.verified_probes:
        candidate = candidates[record.candidate_id]
        probe = candidate.probe
        if probe is None or candidate.time != time or probe_observation_dependencies(observations, probe.target_ids) != record.dependencies:
            continue
        if probe.dependency_sha256 is None:
            original = read_planning_package(context.asset(candidate.planning_path))
            probe = bind_probe_dependencies(probe, original)
        if probe.time != candidate.time:
            # 历史换帧候选可能保留原绑定时刻, 最终注入前按正式评审核对实际帧.
            probe = probe.model_copy(update={"time": candidate.time})
        if not all(can_reuse_probe(probe, package, selection, (state.width, state.height, time)) for selection in chosen):
            continue
        try:
            local = derive_local_review_request(scene, probe, observations, probe.target_region, canvas=canvas)
        except NoLocalReviewChecksError:
            # 新的整图标准可能完全移除这个局部目标, 此时不复用旧评审.
            continue
        if _same_local_standard(context, candidate, probe.criteria, freeze_workflow_review(local), scene):
            retained.append(_snippet(context, candidate, probe))
    return tuple(retained)
