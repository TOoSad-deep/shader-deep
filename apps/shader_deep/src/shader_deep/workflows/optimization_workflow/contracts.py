"""跨阶段控制面公开状态与两道人工作业门禁."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from shader_deep.domain.local_probe import LocalProbeBinding  # noqa: TC001  # 持久状态需校验局部试验绑定.
from shader_deep.domain.scene_planning import ObservationRevisionRequest, RiskProbe  # noqa: TC001  # 持久争议和人工试验.
from shader_deep.domain.visual_review import Digest, EvidenceGapKind, FiniteTime, PixelRegion, ReviewModel, ReviewRequest, ReviewVerdict, Text
from shader_deep.infrastructure.llm.budget import DEFAULT_OUTPUT_BUDGET
from shader_deep.workflows.diagnosis import RevisionRoute  # noqa: TC001  # 反序列化门禁动作需要路由枚举.

_Index = Annotated[int, Field(strict=True, ge=0)]
_Channel = Annotated[float, Field(strict=True, ge=0, le=1)]


class _ProbeDecision(ReviewModel):
    """复用规划中一项高风险试验, 显式补齐可比视图和上下文."""

    probe_id: _Index | None = None
    target_region: PixelRegion | None = None
    solid_rgba: tuple[_Channel, _Channel, _Channel, _Channel] | None = None
    proxy_context: Text | None = None
    patch_source: Text | None = None
    expected_revision: _Index | None = None


class PlannedProbeSelection(_ProbeDecision):
    """首轮已批准的局部试验及其固定区域."""

    probe_id: _Index
    target_region: PixelRegion


class ComparisonPlan(ReviewModel):
    """与首个选择对照的另一项整图或局部路线."""

    sketch_id: Text
    alternative_index: _Index | None = None


class GateADecision(_ProbeDecision):
    """选择整图方案、可选局部试验及人工审核后的冻结标准."""

    action: Literal["generate_scene", "run_probe", "compare_plans", "select_comparison", "archive", "revise_observation", "replan", "retry_criteria"]
    sketch_id: Text | None = None
    alternative_index: _Index | None = None
    probes: tuple[PlannedProbeSelection, ...] = Field(default=(), max_length=2)
    comparison_plan: ComparisonPlan | None = None
    candidate_id: Text | None = None
    criteria: ReviewRequest | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def _selection(self) -> Self:
        if self.action == "retry_criteria" and self.model_fields_set - {"action", "expected_revision", "reason"}:
            msg = "Criteria retry uses the existing observation, planning and budget"
            raise ValueError(msg)
        if self.action in {"run_probe", "generate_scene", "compare_plans"} and self.sketch_id is None:
            msg = "Gate A generation requires an explicit sketch_id"
            raise ValueError(msg)
        if self.probes and (
            self.action != "run_probe" or self.probe_id is not None or len({row.probe_id for row in self.probes}) != len(self.probes)
        ):
            msg = "Choose one or two distinct probes using a single selection format"
            raise ValueError(msg)
        if self.action == "run_probe" and not self.probes and (self.probe_id is None or self.target_region is None):
            msg = "A local probe requires probe_id and target_region"
            raise ValueError(msg)
        if self.action == "compare_plans" and (
            self.comparison_plan is None
            or (self.sketch_id, self.alternative_index) == (self.comparison_plan.sketch_id, self.comparison_plan.alternative_index)
        ):
            msg = "A comparison requires two distinct choices"
            raise ValueError(msg)
        if self.action == "select_comparison" and self.candidate_id is None:
            msg = "Select a reviewed candidate from the current comparison"
            raise ValueError(msg)
        return self


class GateBDecision(_ProbeDecision):
    """接受、修订、改派、回滚或归档, 由控制面校验可执行路线."""

    action: Literal["accept", "revise", "reroute", "rollback", "archive"]
    route: RevisionRoute | None = None
    candidate_id: Text | None = None
    reason: Text | None = None
    time: FiniteTime | None = None
    disputed_ids: tuple[Text, ...] = ()
    probe_override: RiskProbe | None = None

    @model_validator(mode="after")
    def _selection(self) -> Self:
        if self.action == "reroute" and (self.route is None or self.reason is None):
            msg = "Manual rerouting requires route and reason"
            raise ValueError(msg)
        if self.action == "revise" and self.route is not None:
            msg = "Use reroute with a reason to override the recommended revision"
            raise ValueError(msg)
        if self.probe_id is not None and self.probe_override is not None:
            msg = "Select an existing planned probe or provide a new probe binding"
            raise ValueError(msg)
        if (self.probe_id is not None or self.probe_override is not None) and self.target_region is None:
            msg = "A local probe requires target_region"
            raise ValueError(msg)
        return self


class WorkflowCandidate(ReviewModel):
    """实际候选与评审制品的不可变相对索引."""

    id: Text
    native_id: Text
    task_id: Text
    planning_path: Text
    observation_path: Text
    scene_criteria_path: Text
    scene_draft_path: Text | None = None
    scene_criteria_sha256: Digest
    sketch_id: Text
    alternative_index: _Index | None = None
    run_path: Text
    code_path: Text
    preview_path: Text
    render_record_path: Text | None = None
    render_record_sha256: Digest | None = None
    baked_code_path: Text | None = None
    baked_code_sha256: Digest | None = None
    code_sha256: Digest
    preview_sha256: Digest
    vista_record: Text | None = None
    vista_review_record: Text | None = None
    vista_diagnosis_record: Text | None = None
    review_path: Text | None = None
    diagnosis_path: Text | None = None
    diagnosis_sha256: Digest | None = None
    scope: Literal["local", "scene"]
    verdict: ReviewVerdict | None = None
    criteria_sha256: Digest
    review_sha256: Digest | None = None
    major_issues: tuple[Text, ...] = ()
    matched_checks: tuple[Text, ...] = ()
    regressions: tuple[Text, ...] = ()
    remaining_issues: tuple[Text, ...] = ()
    supplemented_review_path: Text | None = None
    evidence_gaps: dict[str, EvidenceGapKind] = Field(default_factory=dict)
    minor_issue_count: _Index = 0
    objective_error: Annotated[float, Field(ge=0, le=1)] | None = None
    objective_run: Text | None = Field(default=None, exclude_if=lambda value: value is None)
    optimization_regions: tuple[PixelRegion, ...] = ()
    time: FiniteTime
    probe: LocalProbeBinding | None = None


class WorkflowAttempt(ReviewModel):
    """为同一稳定必查项保留独立修订的结果."""

    issue_keys: tuple[Text, ...]
    route: RevisionRoute
    unresolved_keys: tuple[Text, ...]
    structure_bottleneck: bool = False
    automatic: bool = False
    candidate_id: Text | None = None
    criteria_sha256: Digest | None = None
    source_candidate_id: Text | None = Field(default=None, exclude_if=lambda value: value is None)
    parent_index: _Index | None = Field(default=None, exclude_if=lambda value: value is None)


class VerifiedProbe(ReviewModel):
    """人工晋升的片段以及它依赖的观察和方法内容身份."""

    candidate_id: Text
    dependencies: dict[str, Digest]


class ObservationCascade(ReviewModel):
    """一次复核所参照的旧制品及需更新的目标和必查项."""

    observation_path: Text
    previous_planning_path: Text
    previous_criteria_path: Text | None = None
    frozen_criteria_sha256: Digest | None = None
    affected_sources: tuple[Text, ...]
    affected_checks: tuple[Text, ...]
    affected_planning_targets: dict[str, tuple[Text, ...]] = Field(default_factory=dict)
    affected_criteria_sources: tuple[Text, ...] = ()
    changed_metadata: tuple[Text, ...] = ()


class WorkflowState(ReviewModel):
    """工作流快照, 记录固定输入、预算、门禁与可回读制品."""

    schema_version: Literal["shader_workflow_v1"] = "shader_workflow_v1"
    revision: _Index = 0
    directory: Text
    phase: Literal["gate_a", "gate_b", "completed", "blocked"] = "gate_a"
    reference_path: Text = "reference.png"
    reference_sha256: Digest
    vista_reference_record: Text | None = None
    vista_history_record: Text | None = None
    request: Text
    width: Annotated[int, Field(strict=True, gt=0)]
    height: Annotated[int, Field(strict=True, gt=0)]
    time: FiniteTime = 0.0
    round: _Index = 0
    max_rounds: Annotated[int, Field(strict=True, ge=1, le=5)] = 4
    max_model_calls: Annotated[int, Field(strict=True, ge=1)] = 100
    remaining_model_calls: _Index
    max_output_tokens: Annotated[int, Field(strict=True, gt=0)] = DEFAULT_OUTPUT_BUDGET
    remaining_output_tokens: _Index = DEFAULT_OUTPUT_BUDGET
    max_total_tokens: Annotated[int, Field(strict=True, gt=0)] | None = None
    remaining_total_tokens: _Index | None = None
    model_context_tokens: Annotated[int, Field(strict=True, gt=0)] | None = None
    reported_input_tokens: _Index = 0
    reported_output_tokens: _Index = 0
    request_attempts: _Index = 0
    unconfirmed_requests: _Index = 0
    budget_stop: Text | None = None
    observation_path: Text | None = None
    observation_versions: tuple[Text, ...] = ()
    changed_observation_ids: tuple[Text, ...] = ()
    observation_cascades: tuple[ObservationCascade, ...] = ()
    planning_path: Text | None = None
    criteria_path: Text | None = None
    frozen_criteria_path: Text | None = None
    frozen_criteria_sha256: Digest | None = None
    observation_rechecks: _Index = 0
    observation_revision_request: ObservationRevisionRequest | None = None
    sketch_id: Text | None = None
    alternative_index: _Index | None = None
    pending_probe: LocalProbeBinding | None = None
    queued_probes: tuple[LocalProbeBinding, ...] = ()
    probe_batch_time: FiniteTime | None = Field(default=None, exclude_if=lambda value: value is None)
    comparison_path: Text | None = None
    comparison_candidate_ids: tuple[Text, ...] = ()
    candidates: tuple[WorkflowCandidate, ...] = ()
    current_candidate: WorkflowCandidate | None = None
    best_candidate: WorkflowCandidate | None = None
    verified_probes: tuple[VerifiedProbe, ...] = ()
    attempts: tuple[WorkflowAttempt, ...] = ()
    attempt_index: _Index | None = Field(default=None, exclude_if=lambda value: value is None)
    escalations: tuple[Text, ...] = ()
    optimization: dict[str, JsonValue] | None = None
    delivery_path: Text | None = None
    manual_best_candidate_id: Text | None = None
    reason: str | None = None
    inflight: str | None = None

    def escalation_routes(self) -> dict[str, tuple[RevisionRoute, RevisionRoute]]:
        """按当前候选、标准和仍未解决的检查项恢复有效升级路线."""
        candidate = self.current_candidate
        if candidate is None:
            return {}
        if self.attempt_index is None:
            return _legacy_escalations(self.attempts, candidate, self.escalations)
        return _escalations_for(_attempt_path(self.attempts, self.attempt_index, candidate), candidate)


def _attempt_path(attempts: tuple[WorkflowAttempt, ...], index: int, candidate: WorkflowCandidate) -> tuple[WorkflowAttempt, ...]:
    """从活动游标沿修订来源回溯, 跳过其他分支上的尝试."""
    path = []
    identity = candidate.id
    while 0 <= index < len(attempts):
        attempt = attempts[index]
        if attempt.candidate_id != identity or attempt.source_candidate_id is None or attempt.criteria_sha256 != candidate.criteria_sha256:
            break
        path.append(attempt)
        if attempt.parent_index is None or attempt.parent_index >= index:
            break
        identity, index = attempt.source_candidate_id, attempt.parent_index
    return tuple(reversed(path))


def _legacy_escalations(
    attempts: tuple[WorkflowAttempt, ...], candidate: WorkflowCandidate, displayed: tuple[str, ...]
) -> dict[str, tuple[RevisionRoute, RevisionRoute]]:
    """旧记录缺少修订来源, 仅保留当前快照已有的升级建议."""
    if not displayed or not attempts or attempts[-1].candidate_id != candidate.id:
        return {}
    history = tuple(attempt for attempt in attempts if attempt.criteria_sha256 in (None, candidate.criteria_sha256))
    routes = _escalations_for(history, candidate)
    return {key: pair for key, pair in routes.items() if f"{key}: {pair[0]} → {pair[1]}" in displayed}


def _escalations_for(history: tuple[WorkflowAttempt, ...], candidate: WorkflowCandidate) -> dict[str, tuple[RevisionRoute, RevisionRoute]]:
    escalated = {}
    for key in dict.fromkeys((*candidate.major_issues, *candidate.remaining_issues)):
        route = _issue_escalation(history, key)
        if route is not None:
            escalated[key] = route
    return escalated


def _issue_escalation(history: tuple[WorkflowAttempt, ...], key: str) -> tuple[RevisionRoute, RevisionRoute] | None:
    """同一问题持续存在时保留升级, 直到解决或再次升级."""
    switch: tuple[RevisionRoute, RevisionRoute] | None = None
    previous = None
    previous_manual = None
    for attempt in history:
        if key not in attempt.issue_keys:
            continue
        if key not in attempt.unresolved_keys:
            previous, previous_manual, switch = None, None, None
            continue
        if attempt.route == "code_structure" and previous_manual == attempt.route:
            switch = ("code_structure", "planning_limit")
        elif switch is None and attempt.route == "numeric_tuning" and (attempt.structure_bottleneck or previous == attempt.route):
            switch = ("numeric_tuning", "code_structure")
        previous = attempt.route
        if not attempt.automatic:
            previous_manual = attempt.route
    return switch
