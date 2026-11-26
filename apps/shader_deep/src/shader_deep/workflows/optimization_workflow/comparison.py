"""在同一轮预算下并行执行两条路线, 为门禁 A 留下可选的正式评审候选."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from shader_deep.domain.tasks import CandidateRecord
from shader_deep.infrastructure.storage.parameterized_candidate import read_candidate_parameters
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.optimization.records import selected_optimization
from shader_deep.workflows.generation_comparison import run_planning_generation_comparison
from shader_deep.workflows.optimization_workflow.candidates import (
    _candidate,
    _require_review,
    _review_findings,
    _save_candidate,
)
from shader_deep.workflows.optimization_workflow.history import archive_generation
from shader_deep.workflows.optimization_workflow.probes import _snippets
from shader_deep.workflows.optimization_workflow.stages import _BudgetExhaustedError
from shader_deep.workflows.optimization_workflow.storage import _relative
from shader_deep.workflows.review import ReviewOptions

if TYPE_CHECKING:
    from shader_deep.domain.generation_comparison import GenerationPlan, PlanningComparisonItem, PlanningGenerationComparison
    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.visual_review import BoundReviewCriteria
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowCandidate, WorkflowState
    from shader_deep.workflows.optimization_workflow.stages import _Context

_MIN_COMPARISON_CALLS = 4


def _compared_candidate(context: _Context, item: PlanningComparisonItem, time: float, probe: LocalProbeBinding | None) -> WorkflowCandidate | None:
    if item.code_path is None or item.preview_path is None or item.selected_candidate_id is None or item.review_report_dir is None:
        return None
    record = CandidateRecord(
        id=item.selected_candidate_id,
        task_id=item.task_id,
        code_path=item.code_path,
        preview_path=item.preview_path,
        render_record_path=item.render_record_path,
    )
    directory = Path(item.run_dir)
    optimized = selected_optimization(directory, record.id)
    if optimized is not None and (
        item.render_record_path is None
        or read_candidate_parameters(Path(item.render_record_path), Path(item.code_path), Path(item.preview_path)).uniforms != optimized.uniforms
    ):
        msg = "Compared optimization values differ from the selected candidate's render record"
        raise ValueError(msg)
    candidate = _candidate(
        context,
        record,
        directory,
        Path(item.baked_code_path) if item.baked_code_path else None,
        time=time,
        probe=probe,
        error=optimized.end_error if optimized else None,
        regions=optimized.evidence_regions if optimized else (),
    )
    package = read_review_package(Path(item.review_report_dir))
    candidate = _review_findings(context, candidate, package)
    _require_review(context, candidate)
    return candidate


def _compare(
    context: _Context,
    plans: tuple[GenerationPlan, GenerationPlan],
    criteria: BoundReviewCriteria,
    probes: tuple[LocalProbeBinding, ...],
) -> WorkflowState:
    if context.state.remaining_model_calls < _MIN_COMPARISON_CALLS:
        msg = "Global comparison budget must cover both generation and review for each plan"
        raise _BudgetExhaustedError(msg)
    context.check_round()
    attempts = probes[0].max_attempts if probes else 4
    time = probes[0].time if probes else context.state.time
    per_plan = attempts + 4 + 3
    # 两项用相同的历史材料, 仅复用对两个生效方案都有效的局部片段.
    snippets = () if probes else _snippets(context, time, compared_with=plans[1])

    def run(calls: int) -> PlanningGenerationComparison:
        per_side = calls // 2
        review_calls = min(3, max(1, per_side // 3))
        return run_planning_generation_comparison(
            context.asset(context.state.planning_path),
            plans,
            options=context.generation_options(calls=per_side - review_calls, attempts=attempts, time=time),
            criteria=probes[0].criteria if probes else criteria,
            local_probes=probes,
            review_options=ReviewOptions(max_model_calls=review_calls),
            verified_snippets=snippets,
            model=context.model,
        )

    record = context.invoke(
        "comparison", 2 * per_plan, run, lambda result, _: sum((row.generation_calls or 0) + (row.review_calls or 0) for row in result.items)
    )
    candidates = []
    for index, item in enumerate(record.items):
        directory = Path(item.run_dir)
        snapshot = directory / "run.json"
        if snapshot.is_file():
            records = TypeAdapter(dict[str, CandidateRecord]).validate_python(json.loads(snapshot.read_bytes())["blackboard"]["candidates"])
            archive_generation(context.directory, directory, records.values(), time)
        context.update(sketch_id=item.plan.sketch_id, alternative_index=item.plan.alternative)
        candidate = _compared_candidate(context, item, time, probes[index] if probes else None)
        if candidate is not None:
            candidates.append(candidate)
    for candidate in candidates:
        _save_candidate(context, candidate, select=False)
    return context.update(
        phase="gate_a",
        sketch_id=None,
        alternative_index=None,
        pending_probe=None,
        current_candidate=None,
        comparison_path=_relative(context.directory, Path(record.directory) / "comparison.json"),
        comparison_candidate_ids=tuple(item.id for item in candidates),
        reason="请查看两项对照并选择一项已评审候选继续。" if candidates else "两项对照均未交付正式评审。请查看对照记录后决定路线。",
    )
