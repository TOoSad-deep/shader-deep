"""执行有界数值微回路、换帧和逐问题连败跟踪."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.scene_observations import REFERENCE_MEASUREMENT_LIMIT
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.diagnosis import _matched_checks
from shader_deep.workflows.numeric_revision import run_frame_revision, run_numeric_revision
from shader_deep.workflows.optimization_workflow.candidates import _candidate, _diagnose, _issue_keys, _material, _review, _save_candidate
from shader_deep.workflows.optimization_workflow.contracts import WorkflowAttempt
from shader_deep.workflows.optimization_workflow.history import archive_render
from shader_deep.workflows.optimization_workflow.stages import _BudgetExhaustedError
from shader_deep.workflows.optimization_workflow.storage import _relative

if TYPE_CHECKING:
    from shader_deep.domain.visual_review import FrozenReviewCheck
    from shader_deep.workflows.diagnosis import RevisionProposal, RevisionRoute
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate, WorkflowState
    from shader_deep.workflows.optimization_workflow.stages import _Context

_ROUTES: tuple[RevisionRoute, ...] = ("observation_error", "planning_limit", "code_structure", "numeric_tuning", "evidence_gap")


def _recommend(proposal: RevisionProposal | None) -> RevisionRoute | None:
    routes = {item.route for item in proposal.items} if proposal is not None else set()
    return next((route for route in _ROUTES if route in routes), None)


def _route_issues(candidate: WorkflowCandidate | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*candidate.major_issues, *candidate.remaining_issues))) if candidate is not None else ()


def _structure_guidance(context: _Context, candidate: WorkflowCandidate | None, reason: str | None, proposal: RevisionProposal | None) -> str | None:
    notes = [f"按人工修订说明处理。{reason}"] if reason else []
    if proposal is not None:
        notes.extend(f"修订靶点为 {item.source_ref}。{item.target}" for item in proposal.items if item.route == "code_structure")
        for guard in proposal.protected_regions:
            box = guard.region
            notes.append(f"保护原图坐标区域 ({box.left}, {box.top}, {box.right}, {box.bottom})。像素漂移容差为 {guard.tolerance:g}。")
    if candidate is not None and candidate.matched_checks:
        package = read_review_package(context.asset(candidate.review_path))
        notes.extend(f"保留 {check.source_ref or 'user_requirement'} 下已匹配的表现。{check.check}" for check in _matched_checks(package))
    return "\n".join(notes) if notes else None


def _track(
    context: _Context,
    original: WorkflowCandidate,
    result: WorkflowCandidate | None,
    route: RevisionRoute,
    *,
    bottleneck: bool = False,
    automatic: bool = False,
    proposal: RevisionProposal | None = None,
) -> None:
    unresolved = _route_issues(result) if result is not None and result.verdict is not None else _route_issues(original)
    issues = _route_issues(original)
    if proposal is not None:
        targets = [item.issue_id for item in proposal.items if item.route == route]
        if targets and len(targets) < len(proposal.items):
            keys = _issue_keys(read_review_package(context.asset(original.review_path)))
            issues = tuple(dict.fromkeys(keys[identity] for identity in targets))
    if not issues:
        return
    state = context.state
    parent = state.attempt_index
    if parent is not None and (parent >= len(state.attempts) or state.attempts[parent].candidate_id != original.id):
        parent = None
    attempt = WorkflowAttempt(
        issue_keys=issues,
        route=route,
        unresolved_keys=tuple(key for key in issues if key in unresolved),
        structure_bottleneck=bottleneck,
        automatic=automatic,
        candidate_id=result.id if result is not None else original.id,
        criteria_sha256=original.criteria_sha256,
        source_candidate_id=original.id,
        parent_index=parent,
    )
    context.update(attempts=(*state.attempts, attempt), attempt_index=len(state.attempts))


def _restored_attempt(state: WorkflowState, candidate: WorkflowCandidate) -> int | None:
    """回到产生所选候选的尝试, 其后未产出候选的尝试也留在历史分支."""
    for index in range(len(state.attempts) - 1, -1, -1):
        attempt = state.attempts[index]
        if attempt.candidate_id == candidate.id and attempt.source_candidate_id not in (None, candidate.id):
            return index
    return None


def _target_regions(context: _Context, candidate: WorkflowCandidate, proposal: RevisionProposal) -> tuple[PixelRegion, ...]:
    report = read_review_package(context.asset(candidate.review_path))
    request = report.source.request
    if request.coverage is None:
        return (candidate.probe.target_region,) if candidate.probe is not None else ()
    keys = _issue_keys(report)
    targets = {keys[item.issue_id] for item in proposal.items if item.route == "numeric_tuning"}
    selected = {
        (check.dimension, check.check)
        for check in report.report.criteria.checks
        if f"{check.source_ref or 'user_requirement'}:{check.check_id}" in targets
    }
    return tuple(dict.fromkeys(item.region for item in request.coverage.required_regions if (item.dimension, item.check) in selected))


def _anchors(context: _Context) -> tuple[PixelRegion, ...]:
    manifest, _ = context.observation()
    return tuple(
        PixelRegion(left=item.region.left, top=item.region.top, right=item.region.right, bottom=item.region.bottom)
        for item in manifest.reference_measurements
        if item.kind == "region_stats"
    )[-REFERENCE_MEASUREMENT_LIMIT:]


def _numeric(context: _Context, candidate: WorkflowCandidate, proposal: RevisionProposal, *, automatic: bool = False) -> WorkflowCandidate | None:
    names = tuple(dict.fromkeys(name for item in proposal.items if item.route == "numeric_tuning" for name in item.parameter_names))
    state = context.state
    regions = _target_regions(context, candidate, proposal)
    anchors = _anchors(context)
    evidence = tuple(dict.fromkeys((*regions, *anchors, *(guard.region for guard in proposal.protected_regions))))
    output = run_numeric_revision(
        _material(context, candidate),
        context.reference(),
        output_dir=context.directory / "revisions",
        width=state.width,
        height=state.height,
        time=candidate.time,
        parameter_names=names,
        regions=regions,
        anchors=anchors,
        protected_regions=proposal.protected_regions,
    )
    for point in output.turning_points:
        archive_render(context.directory, output.run_dir, point, candidate.time, source_agent="optimizer")
    context.update(optimization=output.diagnostics)
    if output.candidate is None:
        if output.diagnostics.get("status") == "baseline_satisfied":
            context.update(reason="原候选已达到数值目标。门禁 B 保留原候选和视觉评审供人工处理其余问题。")
            return None
        _track(context, candidate, None, "numeric_tuning", bottleneck=True, automatic=automatic, proposal=proposal)
        context.update(reason="数值优化遇到结构瓶颈, 仍保留原候选与评审。")
        return None
    start, end = output.diagnostics.get("start_error"), output.diagnostics.get("end_error")
    objective_run = _relative(context.directory, output.run_dir)
    if isinstance(start, (int, float)):
        candidate = candidate.model_copy(update={"objective_error": float(start), "objective_run": objective_run, "optimization_regions": evidence})
        _save_candidate(context, candidate)
    revised = _candidate(
        context,
        output.candidate,
        output.run_dir,
        output.baked_code_path,
        time=candidate.time,
        probe=candidate.probe,
        error=float(end) if isinstance(end, (int, float)) else None,
        regions=evidence,
        source_agent="optimizer",
    ).model_copy(update={"objective_run": objective_run})
    _save_candidate(context, revised)
    result = _review(context, revised)
    _track(context, candidate, result, "numeric_tuning", automatic=automatic, proposal=proposal)
    return result


def _unprotected_checks(context: _Context, candidate: WorkflowCandidate, proposal: RevisionProposal) -> tuple[FrozenReviewCheck, ...]:
    if not candidate.matched_checks:
        return ()
    package = read_review_package(context.asset(candidate.review_path))
    matched = _matched_checks(package)
    coverage = package.source.request.coverage
    if coverage is None:
        return matched
    safe_regions = {guard.region for guard in proposal.protected_regions}
    protected = {(row.dimension, row.check) for row in coverage.required_regions if row.region in safe_regions}
    return tuple(row for row in matched if (row.dimension, row.check) not in protected)


def _auto_numeric(context: _Context, candidate: WorkflowCandidate, proposal: RevisionProposal | None) -> WorkflowCandidate | None:
    if (
        proposal is None
        or candidate.render_record_path is None
        or not candidate.major_issues
        or candidate.remaining_issues
        or not proposal.items
        or any(item.route != "numeric_tuning" or not item.parameter_names for item in proposal.items)
        or not context.state.remaining_model_calls
    ):
        return None
    try:
        unprotected = _unprotected_checks(context, candidate, proposal)
        if unprotected:
            checks = "、".join(row.check for row in unprotected)
            context.update(reason=f"已匹配检查「{checks}」缺少可锁定的原图区域。请在门禁 B 选择修订方式。")
            return None
        return _numeric(context, candidate, proposal, automatic=True)
    except _BudgetExhaustedError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        context.update(reason=f"自动数值调节受阻, 保留候选进入人工门禁。{exc}")
        return None


def _frame(context: _Context, candidate: WorkflowCandidate, time: float) -> WorkflowCandidate:
    state = context.state
    context.update(optimization=None)
    output = run_frame_revision(
        _material(context, candidate), output_dir=context.directory / "revisions", width=state.width, height=state.height, time=time
    )
    if output.candidate is None:
        msg = "Frame revision did not produce a candidate"
        raise RuntimeError(msg)
    probe = candidate.probe.model_copy(update={"time": time}) if candidate.probe is not None else None
    updated = _candidate(context, output.candidate, output.run_dir, output.baked_code_path, time=time, probe=probe, source_agent="renderer")
    context.update(pending_probe=probe)
    _save_candidate(context, updated)
    result = _review(context, updated)
    _diagnose(context, result)
    return result
