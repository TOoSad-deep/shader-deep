"""候选身份、独立评审和跨制品证据装配."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.tasks import CandidateRecord
from shader_deep.domain.visual_review import PixelRegion, ReviewSource
from shader_deep.imaging.review_evidence import missing_sample_regions
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.infrastructure.storage.vista import publish_history_json, read_history_json, read_image
from shader_deep.workflows.diagnosis import RevisionProposal, run_diagnosis
from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate
from shader_deep.workflows.optimization_workflow.history import archive_render, diagnosis_history, verify_render
from shader_deep.workflows.optimization_workflow.stages import _calls, _read_frozen
from shader_deep.workflows.optimization_workflow.storage import _fingerprint, _relative
from shader_deep.workflows.review import run_workflow_review
from shader_deep.workflows.review.options import ReviewOptions

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.visual_review import BoundReviewCriteria
    from shader_deep.infrastructure.storage.review_package import ReviewPackage
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowState
    from shader_deep.workflows.optimization_workflow.stages import _Context

_REVIEW_CALLS = 3


def _candidate(
    context: _Context,
    record: CandidateRecord,
    run_directory: Path,
    baked: Path | None,
    *,
    time: float,
    probe: LocalProbeBinding | None,
    error: float | None = None,
    regions: tuple[PixelRegion, ...] = (),
    source_agent: str = "generation",
) -> WorkflowCandidate:
    code, directory = Path(record.code_path), context.directory
    if record.preview_path is None:
        msg = "Selected candidate is missing its rendered PNG"
        raise ValueError(msg)
    preview = Path(record.preview_path)
    params = Path(record.render_record_path) if record.render_record_path is not None else None
    state = context.state
    frozen = _read_frozen(context)
    criteria = probe.criteria if probe is not None else frozen
    if state.sketch_id is None or state.planning_path is None or state.observation_path is None or state.frozen_criteria_path is None:
        msg = "A candidate requires fixed workflow observation, planning and scene criteria"
        raise ValueError(msg)
    return WorkflowCandidate(
        id=f"{run_directory.name}:{record.id}",
        native_id=record.id,
        task_id=record.task_id,
        code_path=_relative(directory, code),
        planning_path=state.planning_path,
        observation_path=state.observation_path,
        scene_criteria_path=state.frozen_criteria_path,
        scene_draft_path=state.criteria_path,
        scene_criteria_sha256=frozen.criteria_sha256,
        sketch_id=state.sketch_id,
        alternative_index=state.alternative_index,
        run_path=_relative(directory, run_directory),
        code_sha256=_fingerprint(code),
        preview_path=_relative(directory, preview),
        preview_sha256=_fingerprint(preview),
        vista_record=archive_render(directory, run_directory, record, time, source_agent=source_agent),
        render_record_path=_relative(directory, params) if params is not None else None,
        render_record_sha256=_fingerprint(params) if params is not None else None,
        baked_code_path=_relative(directory, baked) if baked else None,
        baked_code_sha256=_fingerprint(baked) if baked is not None else None,
        scope=criteria.request.scope,
        criteria_sha256=criteria.criteria_sha256,
        time=time,
        probe=probe,
        objective_error=error,
        optimization_regions=regions,
    )


def _material(context: _Context, candidate: WorkflowCandidate) -> CandidateRecord:
    code, preview = context.asset(candidate.code_path), context.asset(candidate.preview_path)
    if _fingerprint(code) != candidate.code_sha256 or _fingerprint(preview) != candidate.preview_sha256:
        msg = "Workflow candidate GLSL or PNG has changed"
        raise ValueError(msg)
    parameters = _check_sidecar(context, candidate.render_record_path, candidate.render_record_sha256)
    _check_sidecar(context, candidate.baked_code_path, candidate.baked_code_sha256)
    record = CandidateRecord(
        id=candidate.native_id,
        task_id=candidate.task_id,
        code_path=str(code),
        preview_path=str(preview),
        render_record_path=str(parameters) if parameters is not None else None,
    )
    verify_render(context.directory, candidate, record)
    return record


def _check_sidecar(context: _Context, relative: str | None, digest: str | None) -> Path | None:
    if relative is None and digest is None:
        return None
    path = context.asset(relative)
    if _fingerprint(path) != digest:
        msg = f"Workflow candidate sidecar has changed: {relative}"
        raise ValueError(msg)
    return path


def _require_review(context: _Context, candidate: WorkflowCandidate) -> ReviewPackage:
    _material(context, candidate)
    package = read_review_package(context.asset(candidate.review_path))
    bound = package.source.workflow_criteria
    source = package.source.request.source
    content = dict(package.source.contents)
    if candidate.vista_review_record is not None and read_history_json(context.directory, candidate.vista_review_record) != package.report.model_dump(
        mode="json"
    ):
        msg = "Formal review differs from its immutable workflow archive"
        raise ValueError(msg)
    if context.state.vista_reference_record is not None and read_image(context.directory / "vista", context.state.vista_reference_record)[
        0
    ] != content.get("reference.png"):
        msg = "Formal review differs from its immutable VISTA reference"
        raise ValueError(msg)
    if (
        package.content_sha256 != candidate.review_sha256
        or package.report.verdict != candidate.verdict
        or bound is None
        or bound.criteria_sha256 != candidate.criteria_sha256
        or bound.request.scope != candidate.scope
        or source is None
        or (source.candidate_id != candidate.id and (source.candidate_id != candidate.native_id or source.run_id != Path(candidate.run_path).name))
        or content.get("rendered.png") != context.asset(candidate.preview_path).read_bytes()
        or content.get("reference.png") != context.reference().read_bytes()
    ):
        msg = "Formal review no longer matches the candidate and its frozen criteria"
        raise ValueError(msg)
    return package


def _base_best(context: _Context, candidate: WorkflowCandidate) -> WorkflowCandidate | None:
    comparable = (item for item in context.state.candidates if item.id != candidate.id and _comparable(item, candidate))
    return _preferred(comparable, context.state.manual_best_candidate_id)


def _comparable(item: WorkflowCandidate, candidate: WorkflowCandidate) -> bool:
    return (
        item.verdict is not None
        and item.scope == candidate.scope
        and item.criteria_sha256 == candidate.criteria_sha256
        and item.scene_criteria_sha256 == candidate.scene_criteria_sha256
    )


def _preferred(candidates: Iterable[WorkflowCandidate], marked: str | None) -> WorkflowCandidate | None:
    def score(item: WorkflowCandidate) -> tuple[bool, int, bool, bool, int]:
        return item.id != marked, len(item.major_issues), bool(item.regressions), item.verdict == "inconclusive", len(item.remaining_issues)

    items = tuple(candidates)
    best = min(items, key=score, default=None)
    if best is None or best.objective_run is None:
        return best
    tied = tuple(item for item in items if score(item) == score(best))
    # 仅同一次调参中生成的起点与终点可证明共用目标、锚点和实际帧.
    if all(item.objective_run == best.objective_run and item.time == best.time and item.objective_error is not None for item in tied):
        return min(tied, key=lambda item: item.objective_error if item.objective_error is not None else math.inf)
    return best


def _rollback_best(state: WorkflowState) -> WorkflowCandidate | None:
    """用当前排序重选同一回滚范围, 支持旧快照中已保存的最佳候选."""
    best = state.best_candidate
    if best is None:
        return None
    eligible = (item for item in state.candidates if _comparable(item, best))
    return _preferred(eligible, state.manual_best_candidate_id) or best


def _save_candidate(context: _Context, candidate: WorkflowCandidate, *, select: bool = True) -> None:
    state = context.state
    items = tuple(candidate if row.id == candidate.id else row for row in state.candidates)
    if not any(row.id == candidate.id for row in items):
        items = (*items, candidate)
    prior = state.best_candidate
    marked = state.manual_best_candidate_id
    eligible = (row for row in items if _comparable(row, candidate))
    best = _preferred(eligible, marked)
    # 同一整图标准下, 局部试验不会替换整图回滚点.
    if candidate.scope == "local" and prior is not None and prior.scope == "scene" and prior.scene_criteria_sha256 == candidate.scene_criteria_sha256:
        best = prior
    updates: dict[str, object] = {
        "candidates": items,
        "best_candidate": best,
        "manual_best_candidate_id": marked if best is not None and best.id == marked else None,
    }
    if select:
        updates["current_candidate"] = candidate
    context.update(**updates)


def _issue_keys(package: ReviewPackage) -> dict[str, str]:
    report = package.report
    checks = {(item.dimension, item.check): f"{item.source_ref or 'user_requirement'}:{item.check_id}" for item in report.criteria.checks}
    keys = {
        item.id: checks.get((item.dimension, item.check), f"{item.source_ref or 'user_requirement'}:{item.dimension}")
        for item in report.action_summary.major_issues
    }
    keys.update({item.id: checks.get((item.dimension, item.subject), item.id) for item in report.action_summary.unassessed})
    return keys


def _review_findings(context: _Context, candidate: WorkflowCandidate, package: ReviewPackage) -> WorkflowCandidate:
    report = package.report
    keys = _issue_keys(package)
    major = tuple(keys[item.id] for item in report.action_summary.major_issues)
    pending = tuple(keys[item.id] for item in report.action_summary.unassessed)
    correct = {item.check_id for item in report.check_answers or () if not item.findings and item.unassessed_reason is None}
    matching = tuple(f"{item.source_ref or 'user_requirement'}:{item.check_id}" for item in report.criteria.checks if item.check_id in correct)
    previous = _base_best(context, candidate)
    return candidate.model_copy(
        update={
            "review_path": _relative(context.directory, package.directory),
            "review_sha256": package.content_sha256,
            "vista_review_record": publish_history_json(context.directory, report.model_dump(mode="json")),
            "verdict": report.verdict,
            "major_issues": major,
            "matched_checks": matching,
            "remaining_issues": pending,
            "evidence_gaps": {key: gap.kind for key, gap in package.evidence_gaps.items()},
            "minor_issue_count": report.action_summary.minor_issue_count,
            "regressions": tuple(item for item in previous.matched_checks if item not in matching) if previous is not None else (),
        }
    )


def _resampling(package: ReviewPackage) -> tuple[bool, tuple[PixelRegion, ...]] | None:
    source, summary = package.source, package.report.action_summary
    if package.report.verdict != "inconclusive" or summary.major_issues or not summary.unassessed:
        return None
    sizes = (source.reference_size, source.rendered_size)
    gaps = package.evidence_gaps
    if any(gap.kind != "insufficient_crop_or_sample" for gap in gaps.values()):
        return None
    requested = tuple(gap.sample_region for gap in gaps.values() if gap.sample_region is not None)
    samples = missing_sample_regions(source.request, requested, sizes)
    expand = source.request.reference_region is not None or source.request.rendered_region is not None
    return (expand, samples) if expand or samples else None


def _review_once(
    context: _Context, candidate: WorkflowCandidate, criteria: BoundReviewCriteria, *, expand: bool, samples: tuple[PixelRegion, ...] = ()
) -> tuple[WorkflowCandidate, ReviewPackage | None]:
    record = _material(context, candidate)
    manifest, _ = context.observation()
    outcome = context.invoke(
        "review expansion" if expand or samples else "review",
        _REVIEW_CALLS,
        lambda calls: run_workflow_review(
            context.reference(),
            Path(record.preview_path or ""),
            criteria,
            options=ReviewOptions(max_model_calls=calls, output_dir=context.directory / "review"),
            reference_measurements=manifest.reference_measurements,
            optimization_regions=candidate.optimization_regions,
            source=ReviewSource(candidate_id=candidate.id, run_id=Path(candidate.run_path).name),
            expand_views=expand,
            resample_regions=samples,
            rendered_time=candidate.time,
        ),
        lambda outcome, reserv: _calls(outcome.execution.get("model_calls"), reserv),
    )
    if outcome.report_dir is None:
        context.update(reason=outcome.error or f"Review ended {outcome.status}; see {_relative(context.directory, outcome.run_dir)}")
        return candidate, None
    package = read_review_package(outcome.report_dir)
    result = _review_findings(context, candidate, package)
    _save_candidate(context, result)
    return result, package


def _review(context: _Context, candidate: WorkflowCandidate) -> WorkflowCandidate:
    criteria = candidate.probe.criteria if candidate.probe is not None else _read_frozen(context)
    candidate, package = _review_once(context, candidate, criteria, expand=False)
    return _supplement_review(context, candidate, package)


def _supplement_review(context: _Context, candidate: WorkflowCandidate, package: ReviewPackage | None) -> WorkflowCandidate:
    if package is None or candidate.supplemented_review_path is not None:
        return candidate
    repair = _resampling(package)
    if repair is not None and context.state.remaining_model_calls:
        expand, samples = repair
        criteria = candidate.probe.criteria if candidate.probe is not None else _read_frozen(context)
        candidate = candidate.model_copy(update={"supplemented_review_path": _relative(context.directory, package.directory)})
        _save_candidate(context, candidate)
        candidate, _ = _review_once(context, candidate, criteria, expand=expand, samples=samples)
    return candidate


def _diagnose(context: _Context, candidate: WorkflowCandidate) -> tuple[WorkflowCandidate, RevisionProposal | None]:
    review_dir = candidate.review_path
    if not (candidate.major_issues or candidate.remaining_issues) or review_dir is None:
        return candidate, None
    _require_review(context, candidate)
    state = context.state
    history = diagnosis_history(state)
    diagnosis = context.invoke(
        "diagnosis",
        1,
        lambda _: run_diagnosis(
            context.asset(review_dir),
            context.asset(candidate.planning_path),
            sketch_id=candidate.sketch_id,
            candidate=_material(context, candidate),
            alternative_index=candidate.alternative_index,
            optimization=state.optimization,
            history=history,
            model=context.model,
        ),
        lambda result, _: result.model_calls,
    )
    path = context.asset(candidate.run_path) / f"diagnosis-{context.state.revision:03d}.json"
    write_snapshot(path, diagnosis.model_dump(mode="json"))
    updated = candidate.model_copy(
        update={
            "diagnosis_path": _relative(context.directory, path),
            "diagnosis_sha256": _fingerprint(path),
            "vista_diagnosis_record": publish_history_json(context.directory, diagnosis.model_dump(mode="json")),
        }
    )
    _save_candidate(context, updated)
    return updated, diagnosis


def _read_diagnosis(context: _Context, candidate: WorkflowCandidate, *, apply_escalations: bool = False) -> RevisionProposal | None:
    review = _require_review(context, candidate) if candidate.review_path is not None else None
    path = _check_sidecar(context, candidate.diagnosis_path, candidate.diagnosis_sha256)
    if path is None:
        return None
    proposal = RevisionProposal.model_validate_json(path.read_bytes())
    if candidate.vista_diagnosis_record is not None and read_history_json(context.directory, candidate.vista_diagnosis_record) != proposal.model_dump(
        mode="json"
    ):
        msg = "Diagnosis differs from its immutable workflow archive"
        raise ValueError(msg)
    if proposal.review_sha256 != candidate.review_sha256:
        msg = "Diagnosis no longer matches the candidate review"
        raise ValueError(msg)
    routes = context.state.escalation_routes() if apply_escalations else {}
    if review is not None and routes:
        keys = _issue_keys(review)
        items = []
        for finding in proposal.items:
            switch = routes.get(keys[finding.issue_id])
            if switch is not None and (finding.route == switch[0] or (finding.route == "numeric_tuning" and switch[1] == "planning_limit")):
                target = f"{finding.target} (已有修订未解决本项, 转入 {switch[1]})"
                items.append(finding.model_copy(update={"route": switch[1], "target": target, "parameter_names": ()}))
            else:
                items.append(finding)
        return proposal.model_copy(update={"items": tuple(items)})
    return proposal
