"""以两个显式门禁推进 PNG 到 Shader 的分阶段工作流."""

from __future__ import annotations

import hashlib
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.generation_comparison import GenerationPlan
from shader_deep.domain.scene_planning import ObservationRevisionRequest, effective_planning_sketch
from shader_deep.domain.shader_parameters import bake_uniforms
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.llm.budget import DEFAULT_OUTPUT_BUDGET
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.parameterized_candidate import read_candidate_parameters
from shader_deep.infrastructure.storage.planning_inputs import reference_size
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.infrastructure.storage.vista import publish_image
from shader_deep.optimization.records import selected_optimization
from shader_deep.workflows.generation_from_planning import run_generation_from_planning
from shader_deep.workflows.optimization_workflow.candidates import (
    _candidate,
    _diagnose,
    _material,
    _read_diagnosis,
    _require_review,
    _review,
    _rollback_best,
    _save_candidate,
    _supplement_review,
)
from shader_deep.workflows.optimization_workflow.comparison import _compare
from shader_deep.workflows.optimization_workflow.contracts import GateADecision, GateBDecision, WorkflowState
from shader_deep.workflows.optimization_workflow.history import archive_generation
from shader_deep.workflows.optimization_workflow.probes import _gate_probes, _probe, _remember_probe, _snippets
from shader_deep.workflows.optimization_workflow.revisions import (
    _auto_numeric,
    _frame,
    _numeric,
    _recommend,
    _restored_attempt,
    _structure_guidance,
    _track,
)
from shader_deep.workflows.optimization_workflow.stages import (
    _BudgetExhaustedError,
    _Context,
    _prepare_criteria,
    _read_draft,
    _read_frozen,
    _require_criteria_retry,
    _StageError,
    _upstream,
)
from shader_deep.workflows.optimization_workflow.storage import _lock, _relative, _save, _trace, read_workflow
from shader_deep.workflows.planning.review_criteria import validate_criteria_sources
from shader_deep.workflows.review import freeze_workflow_review

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import BaseChatModel

    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.visual_review import ReviewRequest
    from shader_deep.workflows.diagnosis import RevisionProposal, RevisionRoute
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate

_GENERATION_CALLS = 10


def _blocked(context: _Context, reason: str) -> WorkflowState:
    return context.update(phase="blocked", reason=reason, inflight=None)


def _checkpoint(context: _Context, state: WorkflowState) -> WorkflowState:
    """各阶段将已取得的制品登记完毕后, 在门禁前落实全局止损."""
    if state.budget_stop:
        return _blocked(context, state.budget_stop)
    exhausted = not state.remaining_model_calls or not state.remaining_output_tokens or state.remaining_total_tokens == 0
    if state.phase in {"gate_a", "gate_b"} and exhausted:
        return _blocked(context, "Global model budget exhausted; archived available candidates and outstanding findings")
    return state


def start_workflow(
    reference_path: Path,
    request: str,
    *,
    output_dir: Path | None = None,
    max_rounds: int = 4,
    max_model_calls: int = 100,
    max_output_tokens: int = DEFAULT_OUTPUT_BUDGET,
    max_total_tokens: int | None = None,
    model_context_tokens: int | None = None,
    time: float = 0.0,
    model: BaseChatModel | None = None,
) -> WorkflowState:
    """固定参考 PNG 与总预算, 运行观察、规划和标准草案后停在门禁 A.

    Args:
        reference_path: 本地固定参考 PNG.
        request: 用户对整图的要求.
        output_dir: 独立流程运行目录的父路径.
        max_rounds: 人工进入生成或修订的大轮次上限.
        max_model_calls: 本次工作流逻辑模型调用总上限.
        max_output_tokens: 全链路可分配给实际模型请求的输出 token 总量.
        max_total_tokens: 可选的输入与输出总量上限, 需提供部署的上下文硬上界.
        model_context_tokens: 所有参与模型的最大单请求上下文容量, 包含输入及输出.
        time: 初始基线渲染时刻.
        model: 可注入观察、规划、标准整理及诊断模型.

    Returns:
        持久化工作流状态, 含下一道人工门禁或受阻原因.
    """
    if (max_total_tokens is None) != (model_context_tokens is None):
        msg = "Total-token budgeting requires both max_total_tokens and model_context_tokens"
        raise ValueError(msg)
    content = Path(reference_path).read_bytes()
    width, height = reference_size(content)
    directory = create_run_directory(output_dir)
    state = WorkflowState(
        directory=str(directory),
        request=request,
        reference_sha256=hashlib.sha256(content).hexdigest(),
        vista_reference_record=publish_image(directory / "vista", label="reference", png=content),
        width=width,
        height=height,
        time=time,
        max_rounds=max_rounds,
        max_model_calls=max_model_calls,
        remaining_model_calls=max_model_calls,
        max_output_tokens=max_output_tokens,
        remaining_output_tokens=max_output_tokens,
        max_total_tokens=max_total_tokens,
        remaining_total_tokens=max_total_tokens,
        model_context_tokens=model_context_tokens,
    )
    (directory / state.reference_path).write_bytes(content)
    with _lock(directory):
        context = _Context(_save(state), model)
        try:
            return _checkpoint(context, _upstream(context, observe=True))
        except (_StageError, _BudgetExhaustedError) as exc:
            return _blocked(context, str(exc))


def _start_generation(context: _Context, probe: LocalProbeBinding | None) -> LocalProbeBinding | None:
    state = context.state
    if probe is not None and state.probe_batch_time is not None:
        probe = probe.model_copy(update={"time": state.probe_batch_time})
    context.check_round()
    context.update(
        phase="gate_b",
        pending_probe=probe,
        queued_probes=state.queued_probes if probe is not None else (),
        current_candidate=None,
        reason=None,
        optimization=None,
        probe_batch_time=state.probe_batch_time if probe is not None else None,
    )
    return probe


def _generate(
    context: _Context,
    probe: LocalProbeBinding | None = None,
    *,
    previous: WorkflowCandidate | None = None,
    guidance: str | None = None,
    time: float | None = None,
    on_review: Callable[[WorkflowCandidate], None] | None = None,
) -> WorkflowCandidate | None:
    state = context.state
    sketch = state.sketch_id
    if sketch is None:
        msg = "Workflow generation requires a frozen Gate A selection"
        raise ValueError(msg)
    probe = _start_generation(context, probe)
    attempts = probe.max_attempts if probe is not None else 4
    if time is None:
        time = probe.time if probe is not None else previous.time if previous is not None else state.time
    outcome = context.invoke(
        "generation",
        min(_GENERATION_CALLS, attempts + 4),
        lambda calls: run_generation_from_planning(
            context.asset(state.planning_path),
            sketch_id=sketch,
            alternative_index=state.alternative_index,
            options=context.generation_options(calls=calls, attempts=attempts, time=time),
            verified_snippets=_snippets(context, time) if probe is None else (),
            local_probe=probe,
            revision_candidate=_material(context, previous) if previous is not None else None,
            revision_guidance=guidance,
        ),
        lambda result, _: result.model_calls,
    )
    archive_generation(context.directory, outcome.run_dir, outcome.state["candidates"].values(), time)
    if outcome.selected_candidate is None:
        reason = f"Generation ended {outcome.stop_reason}; see {_relative(context.directory, outcome.run_dir)}."
        if outcome.stop_reason == "blocked":
            stopped = outcome.state["results"].get(f"{outcome.run_dir.name}-blocked")
            if stopped is not None and stopped.status == "blocked":
                reason = f"{stopped.summary} ({stopped.recommendation or 'blocked'}). {reason}"
            if probe is None:
                _blocked(context, reason)
                return None
        context.update(current_candidate=None, reason=reason)
        return None
    optimization = selected_optimization(outcome.run_dir, outcome.selected_candidate.id)
    candidate = _candidate(
        context,
        outcome.selected_candidate,
        outcome.run_dir,
        outcome.baked_code_path,
        time=time,
        probe=probe,
        error=optimization.end_error if optimization is not None else None,
        regions=optimization.evidence_regions if optimization is not None else (),
    )
    if optimization is not None:
        material = _material(context, candidate)
        if material.render_record_path is None:
            msg = "A selected optimization record requires its matching candidate parameters"
            raise ValueError(msg)
        parameters = read_candidate_parameters(
            Path(material.render_record_path),
            context.asset(candidate.code_path),
            context.asset(candidate.preview_path),
        )
        if optimization.uniforms != parameters.uniforms:
            msg = "Selected optimization values differ from the candidate render record"
            raise ValueError(msg)
        context.update(optimization=optimization.model_dump(mode="json"))
    _save_candidate(context, candidate)
    return _evaluate(context, candidate, on_review=on_review)


def _evaluate(context: _Context, candidate: WorkflowCandidate, *, on_review: Callable[[WorkflowCandidate], None] | None = None) -> WorkflowCandidate:
    reviewed = _review(context, candidate)
    if on_review is not None:
        on_review(reviewed)
    return _after_review(context, reviewed)


def _after_review(context: _Context, result: WorkflowCandidate) -> WorkflowCandidate:
    if not context.state.remaining_model_calls:
        return result
    result, proposal = _diagnose(context, result)
    revised = _auto_numeric(context, result, proposal)
    if revised is not None and (revised.major_issues or revised.remaining_issues):
        context.update(reason="定向调参复评仍有未解决条目, 需人工决定下一条路线。")
        if context.state.remaining_model_calls:
            revised, _ = _diagnose(context, revised)
    return revised if revised is not None else result


def _gate_a(context: _Context, choice: GateADecision) -> WorkflowState:
    state = context.state
    if choice.action == "archive":
        return _blocked(context, choice.reason or "Archived at Gate A")
    if choice.action == "select_comparison":
        if choice.candidate_id not in state.comparison_candidate_ids:
            msg = "Select a reviewed candidate from the current comparison"
            raise ValueError(msg)
        candidate = _by_id(state, choice.candidate_id)
        if candidate is None or candidate.planning_path != state.planning_path or candidate.scene_criteria_sha256 != state.frozen_criteria_sha256:
            msg = "The comparison candidate is no longer bound to the active plan and criteria"
            raise ValueError(msg)
        _restore(context, candidate)
        optimized = selected_optimization(context.asset(candidate.run_path), candidate.native_id)
        context.update(phase="gate_b", optimization=optimized.model_dump(mode="json") if optimized else None)
        _after_review(context, _supplement_review(context, candidate, _require_review(context, candidate)))
        return context.state
    if choice.action in {"replan", "revise_observation"}:
        disputed = state.observation_revision_request
        if choice.action == "revise_observation" and disputed is None:
            msg = "Gate A has no observation dispute awaiting manual review"
            raise ValueError(msg)
        context.check_round()
        feedback = (choice.reason,) if choice.reason is not None else ()
        return _upstream(
            context, observe=choice.action == "revise_observation", revision=disputed, feedback=feedback, manual_replan=choice.action == "replan"
        )
    return _select(context, choice)


def _validate_scene_regions(request: ReviewRequest, canvas: PixelRegion) -> None:
    coverage = request.coverage
    if coverage is not None:
        if coverage.reference_window != canvas or coverage.rendered_window != canvas:
            msg = "Workflow coverage must use the shared, zero-origin full canvas"
            raise ValueError(msg)
        if any(not canvas.covers(row.region) for row in coverage.required_regions):
            msg = "Workflow required regions must fit the fixed canvas"
            raise ValueError(msg)
    for sample in request.samples:
        for role, region in (("reference", sample.reference_region), ("rendered", sample.rendered_region)):
            if not canvas.covers(region):
                msg = f"Workflow sample {sample.label!r} {role} region must fit the fixed canvas"
                raise ValueError(msg)


def _select(context: _Context, choice: GateADecision) -> WorkflowState:
    state = context.state
    sketch = choice.sketch_id
    if sketch is None:
        msg = "Gate A requires an explicit sketch"
        raise ValueError(msg)
    package = read_planning_package(context.asset(state.planning_path))
    _, observations = context.observation()
    effective_planning_sketch(package.report, observations, sketch, alternative_index=choice.alternative_index)
    draft = choice.criteria if choice.criteria is not None else _read_draft(context)
    if draft.scope != "scene" or draft.requirements != state.request:
        msg = "Gate A criteria must evaluate the same fixed scene requirement"
        raise ValueError(msg)
    canvas = PixelRegion(left=0, top=0, right=state.width, bottom=state.height)
    _validate_scene_regions(draft, canvas)
    validate_criteria_sources(draft, observations)
    criteria = freeze_workflow_review(draft)
    probes = _gate_probes(context, choice, package, criteria, sketch)
    rival = choice.comparison_plan if choice.action == "compare_plans" else None
    probe = probes[0] if probes else None
    path = context.directory / f"criteria-frozen-{context.state.revision:03d}.json"
    write_snapshot(path, criteria.model_dump(mode="json"))
    best = state.best_candidate
    keep_best = best is not None and best.scene_criteria_sha256 == criteria.criteria_sha256
    context.update(
        sketch_id=sketch,
        alternative_index=choice.alternative_index,
        frozen_criteria_path=_relative(context.directory, path),
        frozen_criteria_sha256=criteria.criteria_sha256,
        phase="gate_a" if rival is not None else "gate_b",
        pending_probe=probe,
        queued_probes=probes[1:] if rival is None else (),
        probe_batch_time=None,
        comparison_path=None,
        comparison_candidate_ids=(),
        best_candidate=best if keep_best else None,
        manual_best_candidate_id=state.manual_best_candidate_id if keep_best else None,
    )
    if rival is not None:
        plans = (
            GenerationPlan(name="方案 A", sketch_id=sketch, alternative=choice.alternative_index),
            GenerationPlan(name="方案 B", sketch_id=rival.sketch_id, alternative=rival.alternative_index),
        )
        return _compare(context, plans, criteria, probes)
    _generate(context, probe)
    return context.state


def _by_id(state: WorkflowState, identity: str | None) -> WorkflowCandidate | None:
    if identity is None:
        return state.current_candidate
    for candidate in state.candidates:
        if candidate.id == identity:
            return candidate
    msg = "Selected candidate does not belong to this workflow"
    raise ValueError(msg)


def _restore(context: _Context, candidate: WorkflowCandidate) -> None:
    _require_review(context, candidate)
    context.update(
        current_candidate=candidate,
        attempt_index=_restored_attempt(context.state, candidate),
        escalations=(),
        observation_path=candidate.observation_path,
        planning_path=candidate.planning_path,
        criteria_path=candidate.scene_draft_path,
        frozen_criteria_path=candidate.scene_criteria_path,
        frozen_criteria_sha256=candidate.scene_criteria_sha256,
        sketch_id=candidate.sketch_id,
        alternative_index=candidate.alternative_index,
        pending_probe=candidate.probe,
        queued_probes=(),
        probe_batch_time=None,
        optimization=None,
        reason=None,
    )


def _accept(context: _Context, candidate: WorkflowCandidate, reason: str | None) -> WorkflowState:
    if candidate.verdict is None or ((candidate.verdict != "no_major_issue_observed" or candidate.regressions) and reason is None):
        msg = "Acceptance requires a formal review and a written reason if findings or regressions remain"
        raise ValueError(msg)
    if candidate.scene_criteria_sha256 != context.state.frozen_criteria_sha256:
        msg = "Restore the candidate's frozen scene criteria before accepting it"
        raise ValueError(msg)
    if candidate.probe is not None:
        _require_batch_frame(context, candidate.time)
        _remember_probe(context, candidate)
        if context.state.queued_probes:
            following, *remaining = context.state.queued_probes
            context.update(queued_probes=tuple(remaining), probe_batch_time=candidate.time)
            _generate(context, following)
        elif candidate.probe.context_mode == "scene_baseline_patch":
            _promote_patch(context, candidate)
        else:
            _generate(context, time=candidate.time)
        return context.state
    _require_review(context, candidate)
    return _deliver(context, candidate, reason)


def _require_batch_frame(context: _Context, time: float) -> None:
    batch_time = context.state.probe_batch_time
    if batch_time is not None and time != batch_time:
        msg = f"本批次已有局部试验在 t={batch_time:g} 通过评审, 后续试验须使用同一帧才能共同回流整图"
        raise ValueError(msg)


def _promote_patch(context: _Context, candidate: WorkflowCandidate) -> None:
    context.check_round()
    criteria = _read_frozen(context)
    fields = {
        "id": f"{candidate.id}:scene-{context.state.revision}",
        "criteria_sha256": criteria.criteria_sha256,
        "scope": "scene",
        "probe": None,
        "review_path": None,
        "review_sha256": None,
        "diagnosis_path": None,
        "diagnosis_sha256": None,
        "vista_review_record": None,
        "vista_diagnosis_record": None,
        "verdict": None,
        "major_issues": (),
        "matched_checks": (),
        "regressions": (),
        "remaining_issues": (),
        "evidence_gaps": {},
        "supplemented_review_path": None,
        "minor_issue_count": 0,
    }
    scene = candidate.model_copy(update=fields)
    context.update(pending_probe=None, probe_batch_time=None, optimization=None)
    _save_candidate(context, scene)
    _evaluate(context, scene)


def _deliver(context: _Context, candidate: WorkflowCandidate, reason: str | None) -> WorkflowState:
    record = _material(context, candidate)
    destination = context.directory / "delivery"
    code, preview = context.asset(candidate.code_path), context.asset(candidate.preview_path)
    source = code.read_text(encoding="utf-8")
    values = {}
    specs = ()
    if record.render_record_path is not None:
        parameter_record = Path(record.render_record_path)
        parameters = read_candidate_parameters(parameter_record, code, preview)
        values = parameters.uniforms
        specs = parameters.specs
    baked = bake_uniforms(source, specs, values)
    destination.mkdir(exist_ok=True)
    (destination / "shader.glsl").write_text(source, encoding="utf-8")
    (destination / "preview.png").write_bytes(preview.read_bytes())
    (destination / "shader.baked.glsl").write_text(baked, encoding="utf-8")
    write_snapshot(destination / "uniforms.json", values)
    write_snapshot(destination / "acceptance.json", {"candidate_id": candidate.id, "review_sha256": candidate.review_sha256, "reason": reason})
    return context.update(
        phase="completed",
        current_candidate=candidate,
        best_candidate=candidate,
        manual_best_candidate_id=candidate.id,
        delivery_path=_relative(context.directory, destination),
        reason=reason,
    )


def _observation_request(context: _Context, decision: GateBDecision, proposal: RevisionProposal | None) -> ObservationRevisionRequest:
    state = context.state
    proposal_items = tuple(item for item in proposal.items if item.route == "observation_error") if proposal else ()
    ids = decision.disputed_ids or tuple(dict.fromkeys(item.source_ref for item in proposal_items if item.source_ref != "user_requirement"))
    contradiction = decision.reason or " / ".join(item.target for item in proposal_items)
    whole = PixelRegion(left=0, top=0, right=state.width, bottom=state.height)
    request = ObservationRevisionRequest(disputed_ids=ids, contradiction=contradiction, suggested_region=decision.target_region or whole)
    _, observations = context.observation()
    request.validate_against(observations, (state.width, state.height))
    return request


def _feedback(context: _Context, candidate: WorkflowCandidate | None, choice: GateBDecision, proposal: RevisionProposal | None) -> tuple[str, ...]:
    notes = [choice.reason] if choice.reason else []
    if proposal is not None:
        notes.extend(f"{item.source_ref}: {item.target}" for item in proposal.items if item.route == "planning_limit")
    if candidate is not None:
        if candidate.probe is not None:
            notes.append(f"局部检验 {candidate.probe.target_ids} 使用 {candidate.probe.context_mode}, 评审未通过 {candidate.major_issues}。")
    elif context.state.pending_probe is not None:
        notes.append(f"局部检验 {context.state.pending_probe.target_ids} 未产出候选。{context.state.reason or ''}")
    return tuple(note[:4096] for note in notes[:8])


def _planning_revision(context: _Context, candidate: WorkflowCandidate | None, choice: GateBDecision, proposal: RevisionProposal | None) -> None:
    state = context.state
    if state.pending_probe is not None and state.alternative_index is None:
        package = read_planning_package(context.asset(state.planning_path))
        selected = next((sketch for sketch in package.report.sketches if sketch.id == state.sketch_id), None)
        if selected is not None and selected.alternatives:
            context.update(phase="gate_a", reason="局部试验证伪。门禁 A 可选择一项规划备选后再验证。")
            return
    context.check_round()
    _upstream(context, feedback=_feedback(context, candidate, choice, proposal))


def _reroute(
    context: _Context, choice: GateBDecision, candidate: WorkflowCandidate | None, proposal: RevisionProposal | None, route: RevisionRoute
) -> None:
    if route == "observation_error":
        revision = _observation_request(context, choice, proposal)
        context.check_round()
        _upstream(context, observe=True, revision=revision)
    elif route == "planning_limit":
        _planning_revision(context, candidate, choice, proposal)
    elif route == "code_structure":
        _structure(context, choice, candidate, proposal)
    elif route == "numeric_tuning":
        if candidate is None or proposal is None or not any(item.route == route for item in proposal.items):
            msg = "Numeric revision requires diagnosed, externalized parameter targets"
            raise ValueError(msg)
        context.check_round()
        result = _numeric(context, candidate, proposal)
        if result is not None and context.state.remaining_model_calls:
            _diagnose(context, result)
    else:
        _evidence(context, choice, candidate)


def _structure(context: _Context, choice: GateBDecision, prior: WorkflowCandidate | None, proposal: RevisionProposal | None) -> None:
    state = context.state
    probe = state.pending_probe
    if (choice.probe_id is not None or choice.probe_override is not None) and state.sketch_id is not None:
        package = read_planning_package(context.asset(state.planning_path))
        probe = _probe(context, choice, package, _read_frozen(context), state.sketch_id, state.alternative_index, override=choice.probe_override)
    guidance = _structure_guidance(context, prior, choice.reason, proposal)
    track = partial(_track, context, prior, route="code_structure", proposal=proposal) if prior is not None else None
    result = _generate(context, probe, previous=prior, guidance=guidance, on_review=track)
    if result is None and track is not None:
        track(None)


def _evidence(context: _Context, choice: GateBDecision, candidate: WorkflowCandidate | None) -> None:
    if candidate is None:
        msg = "Evidence revision requires a rendered candidate"
        raise ValueError(msg)
    if choice.time is not None and choice.time != candidate.time:
        _require_batch_frame(context, choice.time)
        context.check_round()
        _frame(context, candidate, choice.time)
    elif choice.probe_id is not None or choice.probe_override is not None:
        _structure(context, choice, candidate, None)
    elif candidate.probe is not None:
        _generate(context, previous=candidate, guidance=choice.reason or "使用完整场景重新采集局部试验缺少的上下文。")
    else:
        msg = "Provide a different time or explicitly select a probe context to collect new evidence"
        raise ValueError(msg)


def _gate_b(context: _Context, choice: GateBDecision) -> WorkflowState:
    state = context.state
    if choice.action == "archive":
        return _blocked(context, choice.reason or "Archived at Gate B")
    candidate = _by_id(state, choice.candidate_id)
    if choice.action == "rollback":
        selected = candidate if choice.candidate_id is not None else _rollback_best(state)
        if selected is None or selected.verdict is None:
            msg = "No reviewed candidate can be restored"
            raise ValueError(msg)
        _restore(context, selected)
        return context.update(
            best_candidate=selected, manual_best_candidate_id=selected.id, reason=choice.reason or "Restored the selected reviewed candidate"
        )
    if candidate is not None and candidate.id != getattr(state.current_candidate, "id", None):
        msg = "Restore the selected candidate before accepting or revising it"
        raise ValueError(msg)
    if choice.action == "accept":
        if candidate is None:
            msg = "No rendered candidate can be accepted"
            raise ValueError(msg)
        return _accept(context, candidate, choice.reason)
    proposal = _read_diagnosis(context, candidate, apply_escalations=choice.action == "revise") if candidate is not None else None
    route = choice.route or _recommend(proposal) or ("planning_limit" if state.pending_probe is not None else None)
    if route is None:
        msg = "No diagnostic route is available; submit an explicit manual reroute"
        raise ValueError(msg)
    _reroute(context, choice, candidate, proposal, route)
    return context.state


def _retry_criteria(context: _Context, decision: GateADecision) -> WorkflowState:
    state = context.state
    context.reference()
    _require_criteria_retry(context, decision.reason)
    _trace(
        state,
        "gate_submitted",
        action=decision.action,
        previous_draft=state.criteria_path,
        decision=decision.model_dump(mode="json", exclude_none=True),
    )
    try:
        _prepare_criteria(context, redraft_reason=decision.reason)
        return _checkpoint(context, context.state)
    except _BudgetExhaustedError as exc:
        return _blocked(context, str(exc))
    except _StageError as exc:
        if state.phase == "blocked":
            return _blocked(context, str(exc))
        return _checkpoint(context, context.update(reason=str(exc), inflight=None))


def advance_workflow(directory: Path, decision: GateADecision | GateBDecision, *, model: BaseChatModel | None = None) -> WorkflowState:
    """验证一道人工作业门禁, 执行有预算的下一段并保存新状态.

    Args:
        directory: 已启动的整体流程运行目录.
        decision: 对应当前门禁的显式人工动作.
        model: 可注入上游、诊断和并行对照的生成模型.

    Returns:
        后续门禁、已交付状态或受阻记录.

    Raises:
        ValueError: 制品身份已变更, 决策过期或不属于当前门禁.
    """
    root = Path(directory).resolve()
    with _lock(root):
        state = read_workflow(root)
        if decision.expected_revision is not None and decision.expected_revision != state.revision:
            msg = "Workflow state changed; refresh before submitting another decision"
            raise ValueError(msg)
        if isinstance(decision, GateADecision) and decision.action == "retry_criteria":
            return _retry_criteria(_Context(state, model), decision)
        if (state.phase == "gate_a" and isinstance(decision, GateADecision)) or (state.phase == "gate_b" and isinstance(decision, GateBDecision)):
            context = _Context(state, model)
            context.reference()
            _trace(state, "gate_submitted", action=decision.action, decision=decision.model_dump(mode="json", exclude_none=True))
            try:
                if state.inflight:
                    return _blocked(context, f"Interrupted during {state.inflight}; archived the last saved candidates and reserved budget")
                updated = _gate_a(context, decision) if isinstance(decision, GateADecision) else _gate_b(context, decision)
                return _checkpoint(context, updated)
            except _BudgetExhaustedError as exc:
                return _blocked(context, str(exc))
            except _StageError as exc:
                return context.update(reason=str(exc), inflight=None)
        msg = f"Decision does not belong to workflow phase {state.phase}"
        raise ValueError(msg)
