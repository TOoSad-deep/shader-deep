"""复用应用各独立入口, 持久化成本预留和实际产物."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from langchain_core.runnables.config import ensure_config, merge_configs, set_config_context
from pydantic import ValidationError

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.visual_review import DIMENSION_IDS, BoundReviewCriteria, ReviewRequest
from shader_deep.infrastructure.llm.budget import RequestBudget, TokenBalance, meter_requests, workflow_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.observation_package import read_observation_package
from shader_deep.infrastructure.storage.planning_inputs import OBSERVATION_CONTENT_FILES, capture_planning_source, content_sha256, package_asset
from shader_deep.infrastructure.storage.planning_package import read_planning_package, write_planning_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.analysis.contracts import AnalysisRevision
from shader_deep.workflows.analysis.entry import run_analysis
from shader_deep.workflows.optimization_workflow.contracts import ObservationCascade
from shader_deep.workflows.optimization_workflow.storage import _asset, _fingerprint, _relative, _save, _trace
from shader_deep.workflows.options import AnalysisOptions
from shader_deep.workflows.planning.cascade import PlanningRevision, observation_impact
from shader_deep.workflows.planning.entry import run_planning
from shader_deep.workflows.planning.options import PlanningOptions
from shader_deep.workflows.planning.review_criteria import InvalidCriteriaOutputError, draft_scene_review_criteria
from shader_deep.workflows.review import freeze_workflow_review

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import BaseChatModel

    from shader_deep.domain.scene_observations import SceneObservations
    from shader_deep.domain.scene_planning import ObservationRevisionRequest
    from shader_deep.infrastructure.storage.observation_package import ObservationManifest
    from shader_deep.infrastructure.storage.planning_package import PlanningPackage
    from shader_deep.workflows.optimization_workflow.contracts import WorkflowState
    from shader_deep.workflows.outcomes import AnalysisOutcome

_T = TypeVar("_T")
_ANALYSIS_CALLS = 20
_PLANNING_CALLS = 20


class _BudgetExhaustedError(RuntimeError):
    """活动无法在全局调用或大轮预算内启动."""


class _StageError(RuntimeError):
    """已启动的阶段失败, 状态中保留已预留成本和独立运行证据."""


@dataclass
class _Context:
    state: WorkflowState
    model: BaseChatModel | None = None

    def __post_init__(self) -> None:
        self.model = workflow_model(self.model)

    @property
    def directory(self) -> Path:
        return Path(self.state.directory)

    def update(self, **values: object) -> WorkflowState:
        self.state = self.state.model_copy(update={**values, "revision": self.state.revision + 1})
        if "current_candidate" in values or "attempts" in values:
            messages = tuple(f"{key}: {source} → {target}" for key, (source, target) in self.state.escalation_routes().items())
            self.state = self.state.model_copy(update={"escalations": messages})
        self.state = _save(self.state)
        if any(key in values for key in ("phase", "current_candidate", "planning_path", "observation_path", "frozen_criteria_path", "delivery_path")):
            _trace(self.state, "state", changed=sorted(values))
        return self.state

    def asset(self, path: str | None) -> Path:
        if path is None:
            msg = "Workflow stage has no artifact for this decision"
            raise ValueError(msg)
        return _asset(self.directory, path)

    def reference(self) -> Path:
        path = self.asset(self.state.reference_path)
        if _fingerprint(path) != self.state.reference_sha256:
            msg = "Workflow reference PNG has changed"
            raise ValueError(msg)
        return path

    def observation(self) -> tuple[ObservationManifest, SceneObservations]:
        return read_observation_package(self.asset(self.state.observation_path))

    def generation_options(self, *, calls: int, attempts: int = 3, time: float | None = None) -> GenerationOptions:
        state = self.state
        return GenerationOptions(
            width=state.width,
            height=state.height,
            time=state.time if time is None else time,
            max_attempts=attempts,
            max_model_calls=calls,
            output_dir=self.directory / "generation",
            optimize_parameters=True,
            vista_root=self.directory / "vista" if state.vista_reference_record is not None else None,
        )

    def budget(self) -> RequestBudget:
        s = self.state
        balance = TokenBalance(
            s.remaining_output_tokens,
            s.remaining_total_tokens,
            s.reported_input_tokens,
            s.reported_output_tokens,
            s.request_attempts,
            s.unconfirmed_requests,
        )
        return RequestBudget(balance, s.model_context_tokens, self._save_balance)

    def _save_balance(self, balance: TokenBalance) -> None:
        self.update(
            remaining_output_tokens=balance.output,
            remaining_total_tokens=balance.total,
            reported_input_tokens=balance.input_used,
            reported_output_tokens=balance.output_used,
            request_attempts=balance.attempts,
            unconfirmed_requests=balance.unconfirmed,
        )

    def invoke(self, label: str, limit: int, operation: Callable[[int], _T], count: Callable[[_T, int], int]) -> _T:
        if self.state.budget_stop:
            raise _BudgetExhaustedError(self.state.budget_stop)
        available = self.state.remaining_model_calls
        reserved = min(available, limit)
        if reserved == 0:
            msg = f"Global model-call budget exhausted before {label}"
            raise _BudgetExhaustedError(msg)
        # 执行前预留, 进程异常退出后也不会重复使用可能已消耗的额度.
        self.update(remaining_model_calls=available - reserved, inflight=label)
        meter = self.budget()
        _trace(self.state, "stage_start", stage=label, calls_reserved=reserved)
        config = merge_configs(
            ensure_config(), {"metadata": {"shader_workflow": self.directory.name, "workflow_round": self.state.round, "workflow_stage": label}}
        )
        try:
            with meter_requests(meter), set_config_context(config) as stage_context:
                result = stage_context.run(operation, reserved)
            spent = count(result, reserved)
        except Exception as exc:  # 模型及制品阶段都必须保留预算预留和失败事实.
            self.update(inflight=None)
            _trace(
                self.state,
                "stage_error",
                stage=label,
                error=str(exc),
                invalid_output=label == "criteria"
                and (
                    isinstance(exc, InvalidCriteriaOutputError)
                    or (isinstance(exc, ValidationError) and exc.title in {"SceneCriteriaDraft", "ReviewRequest"})
                ),
            )
            if meter.exhausted:
                self.update(budget_stop=meter.exhausted)
                raise _BudgetExhaustedError(meter.exhausted) from exc
            msg = f"{label}: {exc}"
            raise _StageError(msg) from exc
        self.update(remaining_model_calls=available - min(max(0, spent), reserved), inflight=None)
        directory = getattr(result, "run_dir", None) or getattr(result, "directory", None)
        _trace(self.state, "stage_end", stage=label, model_calls=spent, artifact=str(directory) if directory else None)
        if meter.exhausted:
            # 已返回的阶段仍可归档制品, 包括同批对照中先完成的另一侧.
            self.update(budget_stop=meter.exhausted)
        return result

    def check_round(self) -> None:
        if self.state.round >= self.state.max_rounds:
            msg = "Global round budget exhausted; archived the best available candidate"
            raise _BudgetExhaustedError(msg)
        self.update(round=self.state.round + 1)


def _calls(value: object, reserved: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else reserved


def _analysis_calls(outcome: AnalysisOutcome, reserved: int) -> int:
    payload = json.loads((outcome.run_dir / "result.json").read_bytes())
    execution = payload.get("execution") or {}
    return _calls(execution.get("model_calls"), reserved)


def _observation_records(observations: SceneObservations) -> dict[str, str]:
    return {item.id: item.model_dump_json() for group in (observations.elements, observations.features, observations.relations) for item in group}


def _observe(context: _Context, revision: ObservationRevisionRequest | None) -> bool:
    prior_path = context.state.observation_path
    previous = None if revision is None else AnalysisRevision(observation_dir=context.asset(prior_path), request=revision)
    outcome = context.invoke(
        "analysis",
        _ANALYSIS_CALLS,
        lambda calls: run_analysis(
            context.reference(),
            context.state.request,
            options=AnalysisOptions(max_model_calls=calls, output_dir=context.directory / "analysis"),
            revision=previous,
            model=context.model,
        ),
        _analysis_calls,
    )
    if outcome.report_dir is None:
        context.update(phase="blocked", reason=f"Analysis ended {outcome.stop_reason}; see {_relative(context.directory, outcome.run_dir)}")
        return False
    location = _relative(context.directory, outcome.report_dir)
    _, observations = read_observation_package(outcome.report_dir)
    before = _observation_records(context.observation()[1]) if prior_path is not None else {}
    after = _observation_records(observations)
    changed = tuple(sorted(ref for ref in before.keys() | after.keys() if before.get(ref) != after.get(ref)))
    context.update(
        observation_path=location,
        observation_versions=(*context.state.observation_versions, location),
        changed_observation_ids=changed,
        criteria_path=None,
        planning_path=None,
        frozen_criteria_path=None,
        frozen_criteria_sha256=None,
        sketch_id=None,
        alternative_index=None,
        pending_probe=None,
        observation_revision_request=None,
    )
    return True


@dataclass(frozen=True)
class _Previous:
    package: PlanningPackage
    criteria: ReviewRequest | None
    criteria_path: str | None
    criteria_digest: str | None


def _prior_material(context: _Context, plan_path: str | None, criteria_path: str | None, digest: str | None) -> _Previous | None:
    if not plan_path:
        return None
    criteria = None
    if criteria_path is not None:
        data = context.asset(criteria_path).read_bytes()
        if digest is not None:
            frozen = BoundReviewCriteria.model_validate_json(data)
            if frozen.criteria_sha256 != digest:
                msg = "旧冻结标准的身份已改变"
                raise ValueError(msg)
            criteria = frozen.request
        else:
            criteria = ReviewRequest.model_validate_json(data)
    return _Previous(read_planning_package(context.asset(plan_path)), criteria, criteria_path, digest)


def _cascade(context: _Context, previous: _Previous | None, revision: ObservationRevisionRequest | None) -> PlanningRevision | None:
    if previous is None:
        return None
    source = previous.package.source
    impact = observation_impact((source.observation_manifest, source.observations), context.observation(), revision)
    checks = freeze_workflow_review(previous.criteria).checks if previous.criteria is not None else ()
    targets = tuple(ref for ref in impact.planning if ref.startswith(("F", "R")))
    record = ObservationCascade(
        observation_path=str(context.state.observation_path),
        previous_planning_path=_relative(context.directory, previous.package.directory),
        previous_criteria_path=previous.criteria_path,
        frozen_criteria_sha256=previous.criteria_digest,
        affected_sources=impact.planning,
        affected_criteria_sources=impact.criteria,
        changed_metadata=impact.changed_metadata,
        affected_checks=tuple(row.check_id for row in checks if row.source_ref in impact.criteria),
        affected_planning_targets={sketch.id: targets for sketch in previous.package.report.sketches if targets},
    )
    context.update(observation_cascades=(*context.state.observation_cascades, record))
    return PlanningRevision(previous.package.report, impact.planning, impact.changed_metadata)


def _reuse_plan(context: _Context, revision: PlanningRevision) -> None:
    directory = create_run_directory(context.directory / "planning")
    source = capture_planning_source(
        context.asset(context.state.observation_path), directory, context.state.request, generation_options=context.generation_options(calls=1)
    )
    path = write_planning_package(directory / "report", source, revision.previous)
    context.update(phase="gate_a", planning_path=_relative(context.directory, path), observation_revision_request=None, reason=None)


def _plan(context: _Context, feedback: tuple[str, ...] = (), revision: PlanningRevision | None = None, *, confirm_dispute: bool = False) -> None:
    if revision is not None and not revision.affected and not revision.changed_metadata and not feedback and not confirm_dispute:
        _reuse_plan(context, revision)
        return
    try:
        outcome = context.invoke(
            "planning",
            _PLANNING_CALLS,
            lambda calls: run_planning(
                context.asset(context.state.observation_path),
                context.state.request,
                options=PlanningOptions(max_model_calls=calls, output_dir=context.directory / "planning"),
                generation_options=context.generation_options(calls=1),
                model=context.model,
                revision_feedback=feedback,
                revision=revision,
            ),
            lambda outcome, reserv: _calls(outcome.execution.get("model_calls"), reserv),
        )
    except _StageError as exc:
        context.update(phase="blocked", planning_path=None, observation_revision_request=None, reason=str(exc))
        return
    stopped = outcome.report_dir is None and outcome.observation_revision_request is None
    reason = outcome.error
    if stopped and not reason:
        reason = f"Planning ended {outcome.stop_reason}; see {_relative(context.directory, outcome.run_dir)}"
    context.update(
        phase="blocked" if stopped else "gate_a",
        planning_path=_relative(context.directory, outcome.report_dir) if outcome.report_dir is not None else None,
        observation_revision_request=outcome.observation_revision_request,
        reason=reason,
    )


def _prepare_criteria(
    context: _Context,
    previous: ReviewRequest | None = None,
    affected: tuple[str, ...] = (),
    *,
    redraft_reason: str | None = None,
) -> None:
    prior_path = context.state.criteria_path
    if prior_path is not None and redraft_reason is None:
        return
    draft = (
        previous
        if previous is not None and not affected
        else context.invoke(
            "criteria",
            1,
            lambda _: draft_scene_review_criteria(
                context.asset(context.state.observation_path),
                context.state.request,
                model=context.model,
                previous=previous,
                affected_sources=affected,
                redraft_reason=redraft_reason,
            ),
            lambda _result, _: 1,
        )
    )
    path = context.directory / f"criteria-draft-{context.state.revision:03d}.json"
    write_snapshot(path, draft.model_dump(mode="json"))
    if context.state.phase == "blocked" or redraft_reason is not None:
        context.update(criteria_path=_relative(context.directory, path), phase="gate_a", reason=None)
    else:
        context.update(criteria_path=_relative(context.directory, path))
    if redraft_reason is not None:
        _trace(context.state, "criteria_redrafted", previous_draft=prior_path, new_draft=context.state.criteria_path, reason=redraft_reason)


def _legacy_criteria_error(error: str, *, untyped_trace: bool) -> bool:
    # 旧轨迹将这两种业务校验错误标记为 invalid_output=False.
    dimension, _, detail = error.partition(": ")
    return (
        error == "整图标准的必查区域须位于固定参考 PNG 内"
        or (dimension in DIMENSION_IDS and detail == "每个必查项须绑定本轮观察 ID 或 user_requirement")
        or (untyped_trace and re.match(r"^\d+ validation errors? for SceneCriteriaDraft\b", error) is not None)
    )


def _criteria_failed(state: WorkflowState) -> bool:
    """验证最近受阻来自同一观察和规划下的草案校验失败."""
    try:
        path = _asset(Path(state.directory), "workflow-trace.jsonl")
        with path.open(encoding="utf-8") as stream:
            failure, stop = (json.loads(line) for line in deque(stream, maxlen=2))
    except (OSError, ValueError):
        return False
    if not isinstance(failure, dict) or not isinstance(stop, dict):
        return False
    error = failure.get("error")
    invalid_output = failure.get("invalid_output")
    return bool(
        isinstance(error, str)
        and stop.get("event") == "state"
        and stop.get("phase") == "blocked"
        and stop.get("revision") == state.revision
        and failure.get("revision") == state.revision - 1
        and failure.get("event") == "stage_error"
        and failure.get("stage") == "criteria"
        and failure.get("observation") == state.observation_path
        and failure.get("planning") == state.planning_path
        and state.reason == f"criteria: {error}"
        and (
            invalid_output is True
            or (
                (invalid_output is False or "invalid_output" not in failure)
                and _legacy_criteria_error(error, untyped_trace="invalid_output" not in failure)
            )
        )
    )


def _criteria_retry_ready(state: WorkflowState) -> bool:
    return not (
        state.phase not in {"gate_a", "blocked"}
        or state.round
        or state.inflight
        or state.candidates
        or state.sketch_id
        or state.observation_revision_request
        or state.frozen_criteria_path
        or state.frozen_criteria_sha256
        or state.budget_stop
        or not state.remaining_model_calls
        or not state.remaining_output_tokens
        or (state.remaining_total_tokens is not None and state.remaining_total_tokens < (state.model_context_tokens or 1))
        or not state.observation_path
        or not state.planning_path
    )


def _criteria_retry_available(state: WorkflowState) -> bool:
    """供控制面与 Web 判断是否可请求首次草案重试或人工重拟."""
    if not _criteria_retry_ready(state):
        return False
    if state.phase == "gate_a":
        return state.criteria_path is not None
    return state.criteria_path is None and _criteria_failed(state)


def _require_criteria_retry(context: _Context, reason: str | None = None) -> None:
    state = context.state
    if state.phase == "gate_a" and _criteria_retry_ready(state) and (reason is None or not state.criteria_path):
        msg = "Redrafting at Gate A requires a published unfrozen draft and an explicit reason"
        raise ValueError(msg)
    if not _criteria_retry_available(state) or (state.phase == "blocked" and reason is not None):
        msg = "Only an unfrozen first criteria draft blocked by invalid model output can be retried with remaining budget"
        raise ValueError(msg)
    if state.phase == "gate_a" and ((draft := _read_draft(context)).scope != "scene" or draft.requirements != state.request):
        msg = "Redrafting requires the current unfrozen scene draft for this request"
        raise ValueError(msg)
    observation = context.asset(state.observation_path)
    manifest, _ = context.observation()
    package = read_planning_package(context.asset(state.planning_path))
    digest = content_sha256({name: package_asset(observation, name).read_bytes() for name in OBSERVATION_CONTENT_FILES})
    if manifest.reference.sha256 != state.reference_sha256 or digest != package.source.observation_sha256 or package.source.request != state.request:
        msg = "Criteria retry requires the original reference, observation and planning package for this request"
        raise ValueError(msg)


def _upstream(
    context: _Context,
    *,
    observe: bool = False,
    revision: ObservationRevisionRequest | None = None,
    feedback: tuple[str, ...] = (),
    manual_replan: bool = False,
) -> WorkflowState:
    state = context.state
    if state.queued_probes or state.probe_batch_time is not None or state.comparison_candidate_ids or state.comparison_path is not None:
        context.update(queued_probes=(), probe_batch_time=None, comparison_candidate_ids=(), comparison_path=None)
    plan_path, criteria_path, digest = state.planning_path, state.frozen_criteria_path or state.criteria_path, state.frozen_criteria_sha256
    if plan_path is None and state.observation_revision_request and state.observation_cascades:
        prior = state.observation_cascades[-1]
        plan_path, criteria_path, digest = prior.previous_planning_path, prior.previous_criteria_path, prior.frozen_criteria_sha256
    previous = None
    scope = None
    if observe:
        previous = _prior_material(context, plan_path, criteria_path, digest)
        if not _observe(context, revision):
            return context.state
        scope = _cascade(context, previous, revision)
    _plan(context, feedback, scope, confirm_dispute=bool(state.observation_revision_request))
    request = context.state.observation_revision_request
    if request is not None and not context.state.observation_rechecks:
        previous = previous or _prior_material(context, plan_path, criteria_path, digest)
        context.update(observation_rechecks=1)
        if not _observe(context, request):
            return context.state
        scope = _cascade(context, previous, request)
        # 人工重规划的范围由其选择决定, 观察级联仍用于限定标准的更新范围.
        _plan(context, feedback, None if manual_replan else scope, confirm_dispute=True)
    if context.state.planning_path is not None:
        _prepare_criteria(
            context, previous.criteria if previous else None, context.state.observation_cascades[-1].affected_criteria_sources if scope else ()
        )
    return context.state


def _read_draft(context: _Context) -> ReviewRequest:
    return ReviewRequest.model_validate_json(context.asset(context.state.criteria_path).read_bytes())


def _read_frozen(context: _Context) -> BoundReviewCriteria:
    criteria = BoundReviewCriteria.model_validate_json(context.asset(context.state.frozen_criteria_path).read_bytes())
    if criteria.criteria_sha256 != context.state.frozen_criteria_sha256:
        msg = "Frozen workflow criteria has changed"
        raise ValueError(msg)
    return criteria
