"""固定整图观察上的轻量 M/S 合同、生效选择与候选派生."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002  # Pydantic 在运行时生成非空工具 schema.

from shader_deep.domain.scene_observations import ObservationId, ObservationIssue, Text  # noqa: TC001  # Pydantic 运行时解析嵌套字段.
from shader_deep.domain.visual_review import PixelRegion  # noqa: TC001  # Pydantic 运行时解析区域 schema.

if TYPE_CHECKING:
    from shader_deep.domain.scene_observations import SceneObservations

MechanismId = Annotated[str, StringConstraints(strict=True, pattern=r"^M[1-9][0-9]*$")]
SketchId = Annotated[str, StringConstraints(strict=True, pattern=r"^S[1-9][0-9]*$")]
TargetId = Annotated[str, StringConstraints(strict=True, pattern=r"^[FR][1-9][0-9]*$")]
MechanismIds = Annotated[tuple[MechanismId, ...], Field(min_length=1)]
ProbeContextMode = Literal["isolated_solid", "proxy_context", "scene_baseline_patch"]
MARKERS = re.compile(r"\[\[([^\[\]]+)\]\]")


class PlanningModel(BaseModel):
    """新规划字段拒绝自由扩展, 集合采用 tuple 保存."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanningMechanism(PlanningModel):
    """可复用方法及其内在输入条件."""

    id: MechanismId
    name: Text
    method: Text
    conditions: tuple[Text, ...] = ()


class PlanningGap(PlanningModel):
    """引用必要 F/R 的具体实现缺口."""

    refs: Annotated[tuple[TargetId, ...], Field(min_length=1)]
    description: Text


class PlanningAlternative(PlanningModel):
    """相对默认方案替换整组机制的一项备选."""

    choices: Annotated[dict[TargetId, MechanismIds], Field(min_length=1)]
    reason: Text
    conditions: tuple[Text, ...] = ()
    composition: Text | SkipJsonSchema[None] = Field(default_factory=lambda: None)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        # python 模式保留 tuple, JSON 模式转换为数组, 由原始 handler 决定.
        value = cast("dict[str, object]", handler(self))
        if self.composition is None:
            value.pop("composition", None)
        return value

    @field_validator("composition", mode="before")
    @classmethod
    def _provided_composition(cls, value: object) -> object:
        if value is None:
            msg = "composition 省略时继承, 显式提供时必须是非空文本"
            raise ValueError(msg)
        return value


class RiskProbe(PlanningModel):
    """一项由规划提出、等待人工决定是否执行的局部试验."""

    target_ids: Annotated[tuple[ObservationId, ...], Field(min_length=1)]
    reason: Text
    context_mode: ProbeContextMode

    @model_validator(mode="after")
    def _targets(self) -> Self:
        if len(set(self.target_ids)) != len(self.target_ids) or (len(self.target_ids) > 1 and any(ref.startswith("E") for ref in self.target_ids)):
            msg = "局部试验目标须为不重复的 F/R 或一个单独的 E"
            raise ValueError(msg)
        return self


class ObservationRevisionRequest(PlanningModel):
    """规划受阻时要求分析复核的具体矛盾、关联 ID 和建议像素区域."""

    disputed_ids: tuple[ObservationId, ...]
    contradiction: Text
    suggested_region: PixelRegion

    def validate_against(self, observations: SceneObservations, size: tuple[int, int]) -> None:
        """校验争议引用属于本次旧观察且核实区域在原始画布内."""
        known = observations.identities
        disputed = set(self.disputed_ids)
        if len(disputed) != len(self.disputed_ids) or not disputed <= known:
            msg = "复核争议 ID 须唯一且属于固定观察版本"
            raise ValueError(msg)
        if not disputed and known:
            msg = "旧观察已有条目时, 复核必须列出关联的现有 E/F/R ID"
            raise ValueError(msg)
        if self.suggested_region.right > size[0] or self.suggested_region.bottom > size[1]:
            msg = "复核区域须位于原图像素范围内"
            raise ValueError(msg)


class PlanningSketch(PlanningModel):
    """同一完整观察范围内的一套构造方案."""

    id: SketchId
    name: Text
    default: dict[TargetId, MechanismIds]
    composition: Text
    gaps: tuple[PlanningGap, ...] = ()
    alternatives: tuple[PlanningAlternative, ...] = ()
    risk_probes: tuple[RiskProbe, ...] = Field(default=(), exclude_if=lambda value: not value)


class ExtraCandidate(PlanningModel):
    """尚未由默认方案或局部备选表达的共同方法组合."""

    target: TargetId
    mechanisms: MechanismIds
    reason: Text


class ScenePlanningSubmission(PlanningModel):
    """模型维护的规划正文, E/F/R 来自只读绑定观察."""

    mechanisms: tuple[PlanningMechanism, ...]
    sketches: Annotated[tuple[PlanningSketch, ...], Field(min_length=1)]
    extra_candidates: tuple[ExtraCandidate, ...] = ()


class PlanningDiagnostic(PlanningModel):
    """可局部修复的领域问题及 JSON Pointer 位置."""

    code: str
    path: str
    message: str
    related_paths: tuple[str, ...] = ()


class EffectivePlanningSketch(PlanningModel):
    """显式选择后交给生成角色的完整生效材料."""

    sketch_id: SketchId
    name: Text
    alternative_index: int | None = None
    choices: dict[TargetId, MechanismIds]
    composition: Text
    conditions: tuple[Text, ...] = ()
    reason: Text | None = None
    mechanisms: tuple[PlanningMechanism, ...]
    gaps: tuple[PlanningGap, ...]
    observation_gaps: tuple[ObservationIssue, ...]
    complete: bool
    risk_probes: tuple[RiskProbe, ...] = Field(default=(), exclude_if=lambda value: not value)


class PlanningCandidateOrigin(PlanningModel):
    """默认、备选或补充项的可解析来源."""

    sketch_id: SketchId | None = None
    alternative_index: int | None = None
    extra_candidate_index: int | None = None


class PlanningCandidate(PlanningModel):
    """一个目标的一组共同机制及全部来源."""

    target: TargetId
    mechanisms: MechanismIds
    origins: tuple[PlanningCandidateOrigin, ...]


def _issue(code: str, path: str, message: str, related: tuple[str, ...] = ()) -> PlanningDiagnostic:
    return PlanningDiagnostic(code=code, path=path, message=message, related_paths=related)


def _identity_index(report: ScenePlanningSubmission, observations: SceneObservations) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {item.id: [] for group in (observations.elements, observations.features, observations.relations) for item in group}
    for name in ("mechanisms", "sketches"):
        for offset, item in enumerate(getattr(report, name)):
            index.setdefault(item.id, []).append(f"/{name}/{offset}/id")
    return index


def _marked_references(text: str, path: str, allowed: set[str]) -> list[PlanningDiagnostic]:
    return [
        _issue("invalid_body_reference", path, f"正文引用 {identity!r} 不在当前允许范围")
        for identity in MARKERS.findall(text)
        if identity not in allowed
    ]


def _method_diagnostics(report: ScenePlanningSubmission) -> list[PlanningDiagnostic]:
    issues: list[PlanningDiagnostic] = []
    for offset, method in enumerate(report.mechanisms):
        base = f"/mechanisms/{offset}"
        issues.extend(_marked_references(method.method, base + "/method", set()))
        for position, condition in enumerate(method.conditions):
            issues.extend(_marked_references(condition, f"{base}/conditions/{position}", set()))
    return issues


def _choice_diagnostics(choices: dict[str, tuple[str, ...]], path: str, targets: set[str], methods: set[str]) -> list[PlanningDiagnostic]:
    issues: list[PlanningDiagnostic] = []
    for target, identities in choices.items():
        current = path + "/" + target
        if target not in targets:
            issues.append(_issue("invalid_target", current, f"目标 {target} 不在允许的 F/R 范围"))
        for offset, identity in enumerate(identities):
            if identity not in methods:
                issues.append(_issue("missing_mechanism", f"{current}/{offset}", f"机制 {identity} 不存在或不唯一"))
            if identity in identities[:offset]:
                issues.append(_issue("duplicate_reference", f"{current}/{offset}", f"机制 {identity} 重复"))
    return issues


def _gap_diagnostics(sketch: PlanningSketch, path: str, targets: set[str], identities: set[str]) -> list[PlanningDiagnostic]:
    issues: list[PlanningDiagnostic] = []
    covered: set[str] = set(sketch.default)
    for offset, gap in enumerate(sketch.gaps):
        current = f"{path}/gaps/{offset}"
        covered.update(gap.refs)
        for position, identity in enumerate(gap.refs):
            if identity not in targets:
                issues.append(_issue("invalid_gap_target", f"{current}/refs/{position}", f"缺口目标 {identity} 不存在"))
            if identity in gap.refs[:position]:
                issues.append(_issue("duplicate_reference", f"{current}/refs/{position}", f"缺口引用 {identity} 重复"))
        issues.extend(_marked_references(gap.description, current + "/description", identities))
    issues.extend(_issue("uncovered_target", path + "/default", f"必要目标 {missing} 缺少选择或具体缺口") for missing in sorted(targets - covered))
    return issues


def _effective_choices(sketch: PlanningSketch, alternative_index: int | None) -> tuple[dict[str, tuple[str, ...]], str]:
    choices = dict(sketch.default)
    if alternative_index is None:
        return choices, sketch.composition
    alternative = sketch.alternatives[alternative_index]
    choices.update(alternative.choices)
    return choices, alternative.composition if alternative.composition is not None else sketch.composition


def _probe_diagnostics(
    sketch: PlanningSketch, path: str, observations: SceneObservations, *, require_probe_mechanisms: bool
) -> list[PlanningDiagnostic]:
    issues: list[PlanningDiagnostic] = []
    identities = observations.identities
    for index, probe in enumerate(sketch.risk_probes):
        current = f"{path}/risk_probes/{index}/target_ids"
        missing = [(position, identity) for position, identity in enumerate(probe.target_ids) if identity not in identities]
        issues.extend(
            _issue("invalid_probe_target", f"{current}/{position}", f"试验目标 {identity} 不在当前观察版本") for position, identity in missing
        )
        # 默认与备选具有相同目标集合, 每项备选只替换对应机制组合.
        if require_probe_mechanisms and not missing and observations.target_refs(probe.target_ids).isdisjoint(sketch.default):
            issues.append(_issue("unbound_probe_target", current, "试验目标在当前方案中须绑定至少一个机制"))
    return issues


def _sketch_diagnostics(
    sketch: PlanningSketch, path: str, targets: set[str], methods: set[str], identities: set[str], observation_ids: set[str]
) -> list[PlanningDiagnostic]:
    issues = _choice_diagnostics(sketch.default, path + "/default", targets, methods)
    issues.extend(_gap_diagnostics(sketch, path, targets, identities))
    selections: list[tuple[int | None, str, str]] = [(None, sketch.composition, path + "/composition")]
    for offset, alternative in enumerate(sketch.alternatives):
        current = f"{path}/alternatives/{offset}"
        issues.extend(_choice_diagnostics(alternative.choices, current + "/choices", set(sketch.default), methods))
        issues.extend(_marked_references(alternative.reason, current + "/reason", identities))
        for position, condition in enumerate(alternative.conditions):
            issues.extend(_marked_references(condition, f"{current}/conditions/{position}", identities))
        selections.append((offset, _effective_choices(sketch, offset)[1], current + "/composition"))
    for selected, composition, current in selections:
        choices, _ = _effective_choices(sketch, selected)
        selected_methods = {identity for group in choices.values() for identity in group}
        issues.extend(_marked_references(composition, current, observation_ids | (selected_methods & methods)))
    return issues


def _candidate_key(target: str, mechanisms: tuple[str, ...]) -> tuple[str, frozenset[str]]:
    return target, frozenset(mechanisms)


def _candidate_diagnostics(report: ScenePlanningSubmission, targets: set[str], methods: set[str], identities: set[str]) -> list[PlanningDiagnostic]:
    issues: list[PlanningDiagnostic] = []
    represented = {
        _candidate_key(target, group)
        for sketch in report.sketches
        for selected in (None, *range(len(sketch.alternatives)))
        for target, group in _effective_choices(sketch, selected)[0].items()
    }
    for offset, candidate in enumerate(report.extra_candidates):
        path = f"/extra_candidates/{offset}"
        issues.extend(_choice_diagnostics({candidate.target: candidate.mechanisms}, path, targets, methods))
        issues.extend(_marked_references(candidate.reason, path + "/reason", identities))
        key = _candidate_key(candidate.target, candidate.mechanisms)
        if key in represented:
            issues.append(_issue("duplicate_candidate", path, "组合已由方案或先前补充项表达, 核对限制迁移后局部修订"))
        represented.add(key)
    return issues


def diagnose_scene_planning(
    report: ScenePlanningSubmission, observations: SceneObservations, *, require_probe_mechanisms: bool = True
) -> tuple[PlanningDiagnostic, ...]:
    """汇总绑定整图上的字段合法草稿的业务问题.

    Args:
        report: 模型维护的 M/S 草稿.
        observations: 固定版本的整图 E/F/R.
        require_probe_mechanisms: 新规划要求试验目标绑定机制, 旧包仍可回读.

    Returns:
        可定位的身份、引用、覆盖与组合问题.
    """
    current = ScenePlanningSubmission.model_validate(report.model_dump(mode="json", exclude_none=True))
    index = _identity_index(current, observations)
    issues = [_issue("duplicate_id", paths[0], f"ID {identity} 重复", tuple(paths)) for identity, paths in index.items() if len(paths) > 1]
    methods = {method.id for method in current.mechanisms if len(index[method.id]) == 1}
    observation_ids = {item.id for group in (observations.elements, observations.features, observations.relations) for item in group}
    targets = {item.id for group in (observations.features, observations.relations) for item in group}
    issues.extend(_method_diagnostics(current))
    for offset, sketch in enumerate(current.sketches):
        path = f"/sketches/{offset}"
        issues.extend(_sketch_diagnostics(sketch, path, targets, methods, set(index), observation_ids))
        issues.extend(_probe_diagnostics(sketch, path, observations, require_probe_mechanisms=require_probe_mechanisms))
    issues.extend(_candidate_diagnostics(current, targets, methods, set(index)))
    used = {identity for row in build_candidate_index(current) for identity in row.mechanisms}
    issues.extend(
        _issue("unused_mechanism", f"/mechanisms/{offset}/id", f"机制 {method.id} 缺少方案或候选来源")
        for offset, method in enumerate(current.mechanisms)
        if method.id not in used
    )
    return tuple(issues)


def validate_scene_planning(report: ScenePlanningSubmission, observations: SceneObservations, *, require_probe_mechanisms: bool = True) -> None:
    """正式校验全部默认及备选视图.

    Args:
        report: 需要接受或读取的规划正文.
        observations: 同一固定观察版本.
        require_probe_mechanisms: 新规划要求试验目标绑定机制, 旧包仍可回读.

    Raises:
        ValueError: 规划存在可定位业务问题.
    """
    issues = diagnose_scene_planning(report, observations, require_probe_mechanisms=require_probe_mechanisms)
    if issues:
        msg = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise ValueError(msg)


def build_candidate_index(report: ScenePlanningSubmission) -> tuple[PlanningCandidate, ...]:
    """从完整生效视图派生保留所有来源的组合索引.

    Args:
        report: 字段结构合法的规划正文.

    Returns:
        同目标、同机制集合合并后的候选组合.
    """
    rows: dict[tuple[str, frozenset[str]], PlanningCandidate] = {}
    for sketch in report.sketches:
        for selected in (None, *range(len(sketch.alternatives))):
            origin = PlanningCandidateOrigin(sketch_id=sketch.id, alternative_index=selected)
            for target, methods in _effective_choices(sketch, selected)[0].items():
                _add_candidate(rows, target, methods, origin)
    for offset, candidate in enumerate(report.extra_candidates):
        _add_candidate(rows, candidate.target, candidate.mechanisms, PlanningCandidateOrigin(extra_candidate_index=offset))
    return tuple(rows.values())


def _add_candidate(
    rows: dict[tuple[str, frozenset[str]], PlanningCandidate], target: str, methods: tuple[str, ...], origin: PlanningCandidateOrigin
) -> None:
    key = _candidate_key(target, methods)
    previous = rows.get(key)
    rows[key] = PlanningCandidate(
        target=target, mechanisms=previous.mechanisms if previous else methods, origins=(*previous.origins, origin) if previous else (origin,)
    )


def effective_planning_sketch(
    report: ScenePlanningSubmission,
    observations: SceneObservations,
    sketch_id: str,
    *,
    alternative_index: int | None = None,
    upstream_gaps: tuple[ObservationIssue, ...] = (),
) -> EffectivePlanningSketch:
    """解析显式方案及至多一个局部备选, 保留两类缺口.

    Args:
        report: 已提交的规划正文.
        observations: 固定版本的整图观察.
        sketch_id: 本轮 S 身份.
        alternative_index: 可选的零基备选位置.
        upstream_gaps: 固定观察共同继承的必要缺口.

    Returns:
        可送入生成请求的生效选择与条件.

    Raises:
        ValueError: 规划非法或显式选择不存在.
    """
    validate_scene_planning(report, observations, require_probe_mechanisms=False)
    sketch = next((item for item in report.sketches if item.id == sketch_id), None)
    if sketch is None:
        msg = f"规划中不存在草图 {sketch_id!r}"
        raise ValueError(msg)
    if alternative_index is not None and (
        isinstance(alternative_index, bool) or not isinstance(alternative_index, int) or not 0 <= alternative_index < len(sketch.alternatives)
    ):
        msg = "alternative_index 必须指向当前 S 的一项零基备选"
        raise ValueError(msg)
    return _selected_sketch(report, sketch, alternative_index, upstream_gaps)


def _selected_sketch(
    report: ScenePlanningSubmission, sketch: PlanningSketch, selected: int | None, upstream_gaps: tuple[ObservationIssue, ...]
) -> EffectivePlanningSketch:
    choices, composition = _effective_choices(sketch, selected)
    methods = {identity for group in choices.values() for identity in group}
    option = sketch.alternatives[selected] if selected is not None else None
    return EffectivePlanningSketch(
        sketch_id=sketch.id,
        name=sketch.name,
        alternative_index=selected,
        choices=choices,
        composition=composition,
        conditions=option.conditions if option else (),
        reason=option.reason if option else None,
        mechanisms=tuple(item for item in report.mechanisms if item.id in methods),
        gaps=sketch.gaps,
        observation_gaps=upstream_gaps,
        complete=not (sketch.gaps or upstream_gaps),
        risk_probes=sketch.risk_probes,
    )
