"""全图观察的轻量合同与引用校验, 不承担机制规划或文件 IO."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
ElementId = Annotated[str, StringConstraints(strict=True, pattern=r"^E[1-9][0-9]*$")]
FeatureId = Annotated[str, StringConstraints(strict=True, pattern=r"^F[1-9][0-9]*$")]
RelationId = Annotated[str, StringConstraints(strict=True, pattern=r"^R[1-9][0-9]*$")]


class ObservationModel(BaseModel):
    """观察对象拒绝额外字段, 嵌套列表以不可变 tuple 保存."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ObservationIssue(ObservationModel):
    """非空观察问题; 空 refs 表示全图问题."""

    refs: tuple[Text, ...] = ()
    description: Text


class ObservationFeature(ObservationModel):
    """一处可辨认外观或有规律的重复群."""

    id: FeatureId
    description: Text


class ObservationElementSubmission(ObservationModel):
    """提交时以嵌套特征表达唯一主归属."""

    id: ElementId
    name: Text
    region: Text
    features: Annotated[tuple[ObservationFeature, ...], Field(min_length=1)]


class ObservationRelation(ObservationModel):
    """一类关键配合, 可关联同元素或跨元素的 E/F."""

    id: RelationId
    participants: Annotated[tuple[Text, ...], Field(min_length=2)]
    description: Text


class SceneObservationSubmission(ObservationModel):
    """单次完整观察, 不要求证据、机制、草图或重复归属映射."""

    elements: tuple[ObservationElementSubmission, ...]
    context_notes: tuple[Text, ...] = ()
    relations: tuple[ObservationRelation, ...] = ()
    open_questions: tuple[ObservationIssue, ...] = ()
    gaps: tuple[ObservationIssue, ...] = ()


class ObservationElement(ObservationModel):
    """保存用元素, feature_ids 由嵌套提交派生."""

    id: ElementId
    name: Text
    region: Text
    feature_ids: Annotated[tuple[FeatureId, ...], Field(min_length=1)]


class SceneObservations(ObservationModel):
    """同一快照的 E/F/R 集合, 上下文及问题由 manifest 保存."""

    elements: tuple[ObservationElement, ...]
    features: tuple[ObservationFeature, ...]
    relations: tuple[ObservationRelation, ...]


def _require(condition: bool, field: str, reason: str) -> None:  # noqa: FBT001  # 内部断言接收校验谓词, 并非切换行为的布尔选项.
    if not condition:
        msg = f"{field}: {reason}"
        raise ValueError(msg)


def _index(observations: SceneObservations) -> dict[str, str]:
    index: dict[str, str] = {}
    for collection in ("elements", "features", "relations"):
        for item in getattr(observations, collection):
            _require(item.id not in index, f"{collection}.{item.id}", "ID 必须在场景内唯一")
            index[item.id] = collection
    return index


def _validate_ownership(observations: SceneObservations, index: dict[str, str]) -> None:
    owned: set[str] = set()
    for element in observations.elements:
        for identity in element.feature_ids:
            field = f"elements.{element.id}.feature_ids"
            _require(index.get(identity) == "features", field, f"特征 {identity!r} 不存在或类型错误")
            _require(identity not in owned, field, f"特征 {identity!r} 必须恰好有一个主归属")
            owned.add(identity)
    _require(owned == {item.id for item in observations.features}, "features", "存在没有主归属的特征")


def _validate_relations(observations: SceneObservations, index: dict[str, str]) -> None:
    for relation in observations.relations:
        field = f"relations.{relation.id}.participants"
        _require(len(set(relation.participants)) == len(relation.participants), field, "参与项不能重复")
        for identity in relation.participants:
            _require(index.get(identity) in {"elements", "features"}, field, f"参与项 {identity!r} 不存在或类型错误")


def _validate_issues(issues: tuple[ObservationIssue, ...], index: dict[str, str], field: str) -> None:
    for offset, issue in enumerate(issues):
        _require(len(set(issue.refs)) == len(issue.refs), f"{field}[{offset}].refs", "问题引用不能重复")
        for identity in issue.refs:
            _require(identity in index, f"{field}[{offset}].refs", f"引用 {identity!r} 不存在")


def validate_scene_observations(
    observations: SceneObservations,
    *,
    context_notes: tuple[str, ...] = (),
    open_questions: tuple[ObservationIssue, ...] = (),
    gaps: tuple[ObservationIssue, ...] = (),
) -> None:
    """校验摊平观察、主归属与问题引用.

    Args:
        observations: 同一快照的 E/F/R.
        context_notes: 纯背景图必须有非空背景观察.
        open_questions: 待确认的观察问题.
        gaps: 已声明的必要观察缺口.

    Raises:
        ValueError: 字段、归属或引用不合法.
    """
    # model_copy 可绕过 Pydantic 校验, 因此公开校验入口先重建字段合同.
    current = SceneObservations.model_validate(observations.model_dump(mode="json"))
    declaration = SceneObservationSubmission.model_validate(
        {
            "elements": [],
            "context_notes": context_notes,
            "open_questions": [item.model_dump(mode="json") for item in open_questions],
            "gaps": [item.model_dump(mode="json") for item in gaps],
        }
    )
    _require(bool(current.elements or declaration.context_notes), "context_notes", "无元素时必须记录实际背景观察")
    index = _index(current)
    _validate_ownership(current, index)
    _validate_relations(current, index)
    _validate_issues(declaration.open_questions, index, "open_questions")
    _validate_issues(declaration.gaps, index, "gaps")


def build_scene_observations(submission: SceneObservationSubmission) -> SceneObservations:
    """从完整提交派生唯一主归属, 保留短 ID 和提交顺序.

    Args:
        submission: 嵌套元素与特征的完整观察提交.

    Returns:
        已完成结构和引用校验的保存用 E/F/R.

    Raises:
        ValueError: 字段、归属、关系或问题引用不合法.
    """
    report = SceneObservationSubmission.model_validate(submission.model_dump(mode="json"))
    observations = SceneObservations(
        elements=tuple(
            ObservationElement(id=item.id, name=item.name, region=item.region, feature_ids=tuple(feature.id for feature in item.features))
            for item in report.elements
        ),
        features=tuple(feature for item in report.elements for feature in item.features),
        relations=report.relations,
    )
    validate_scene_observations(observations, context_notes=report.context_notes, open_questions=report.open_questions, gaps=report.gaps)
    return observations


class ObservationDiagnostic(ObservationModel):
    """用于草稿修复的结构位置与错误说明."""

    code: str
    path: str
    message: str
    related_paths: tuple[str, ...] = ()


def _submission_index(report: SceneObservationSubmission) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = {}
    for offset, element in enumerate(report.elements):
        index.setdefault(element.id, []).append(("E", f"/elements/{offset}/id"))
        for feature_offset, feature in enumerate(element.features):
            index.setdefault(feature.id, []).append(("F", f"/elements/{offset}/features/{feature_offset}/id"))
    for offset, relation in enumerate(report.relations):
        index.setdefault(relation.id, []).append(("R", f"/relations/{offset}/id"))
    return index


def _reference_diagnostics(
    refs: tuple[str, ...], path: str, index: dict[str, list[tuple[str, str]]], allowed: frozenset[str]
) -> list[ObservationDiagnostic]:
    issues: list[ObservationDiagnostic] = []
    for offset, identity in enumerate(refs):
        target = f"{path}/{offset}"
        locations = index.get(identity, [])
        if identity in refs[:offset]:
            issues.append(ObservationDiagnostic(code="duplicate_reference", path=target, message=f"引用 {identity} 重复"))
        if not locations:
            issues.append(ObservationDiagnostic(code="missing_reference", path=target, message=f"引用 {identity} 不存在"))
        elif len(locations) > 1:
            issues.append(
                ObservationDiagnostic(
                    code="ambiguous_reference", path=target, message=f"引用 {identity} 不唯一", related_paths=tuple(p for _, p in locations)
                )
            )
        elif locations[0][0] not in allowed:
            issues.append(ObservationDiagnostic(code="reference_type", path=target, message=f"引用 {identity} 的类别不符合要求"))
    return issues


def diagnose_scene_observations(submission: SceneObservationSubmission) -> tuple[ObservationDiagnostic, ...]:
    """汇总可定位的草稿业务错误, 保留所有重号对象.

    Args:
        submission: 字段结构合法的嵌套观察草稿.

    Returns:
        编号、引用和背景缺失的诊断列表.
    """
    report = SceneObservationSubmission.model_validate(submission.model_dump(mode="json"))
    index = _submission_index(report)
    issues = [
        ObservationDiagnostic(code="duplicate_id", path=locations[0][1], message=f"ID {identity} 重复", related_paths=tuple(p for _, p in locations))
        for identity, locations in index.items()
        if len(locations) > 1
    ]
    if not report.elements and not report.context_notes:
        issues.append(ObservationDiagnostic(code="background_missing", path="/context_notes", message="无元素时必须记录实际背景观察"))
    for offset, relation in enumerate(report.relations):
        issues.extend(_reference_diagnostics(relation.participants, f"/relations/{offset}/participants", index, frozenset({"E", "F"})))
    for name in ("open_questions", "gaps"):
        for offset, issue in enumerate(getattr(report, name)):
            issues.extend(_reference_diagnostics(issue.refs, f"/{name}/{offset}/refs", index, frozenset({"E", "F", "R"})))
    return tuple(issues)
