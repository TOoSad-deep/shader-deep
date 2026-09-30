"""五库结构与引用规则; 不将校验等同于视觉或机制正确."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.five_libraries.models import Element, ExplorationSubmission, FiveLibraries, FrozenModel, Selections, Sketch
from shader_deep.domain.five_libraries.references import fail, parse_references

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

KINDS = ("elements", "features", "relations", "mechanisms", "sketches")
TEXT_FIELDS = {"name", "region", "description", "method", "conditions", "composition", "reason", "role"}


def object_index(library: FiveLibraries) -> dict[str, str]:
    """建立真正对象类型索引, 拒绝跨库同名."""
    index: dict[str, str] = {}
    for kind in KINDS:
        for item in getattr(library, kind):
            if item.id in index:
                fail(f"{kind}.{item.id}", item.id, "包内对象 ID 重复")
            index[item.id] = kind
    return index


def _unique(ids: Sequence[str], field: str) -> None:
    if len(set(ids)) != len(ids):
        fail(field, ids, "引用不得重复")


def _ref(ref: str, kinds: set[str], index: Mapping[str, str], field: str) -> None:
    if index.get(ref) not in kinds:
        fail(field, ref, f"引用不存在或类型错误; 要求 {sorted(kinds)}")


def _texts(value: object, index: Mapping[str, str], field: str, key: str = "") -> None:
    if isinstance(value, str) and key in TEXT_FIELDS:
        parse_references(value, index, field=field)
    elif isinstance(value, dict):
        for child_key, child in value.items():
            _texts(child, index, f"{field}.{child_key}", str(child_key))
    elif isinstance(value, (tuple, list)):
        for number, child in enumerate(value):
            _texts(child, index, f"{field}[{number}]", key)


def _model_texts(model: FrozenModel, index: Mapping[str, str], field: str) -> None:
    _texts(model.model_dump(), index, field)


def _selections(choices: Selections, library: FiveLibraries, field: str) -> None:
    candidates = {item.id: {ref.id for ref in item.mechanism_refs} for item in (*library.features, *library.relations)}
    for target, mechanisms in choices.items():
        if target not in candidates:
            fail(field, target, "选择目标必须是已有特征或关系")
        if not mechanisms:
            fail(field, target, "共同采用的机制不能为空")
        _unique(mechanisms, f"{field}.{target}")
        if not set(mechanisms) <= candidates[target]:
            fail(field, mechanisms, f"选择超出 {target} 的候选机制")


def _sketch(sketch: Sketch, library: FiveLibraries, target_id: str) -> None:
    field = f"sketches.{sketch.id}"
    if sketch.element_id != target_id:
        fail(field, sketch.element_id, "草图必须针对本轮目标元素")
    _selections(sketch.default, library, f"{field}.default")
    _composition(sketch.composition, sketch.default, library, f"{field}.composition")
    for number, alternative in enumerate(sketch.alternatives or ()):
        alt_field = f"{field}.alternatives[{number}]"
        if not alternative.choices or not alternative.choices.keys() <= sketch.default.keys():
            fail(alt_field, alternative.choices, "备选必须非空并且只替换默认目标")
        _selections(alternative.choices, library, f"{alt_field}.choices")
        effective = {**sketch.default, **alternative.choices}
        composition = alternative.composition or sketch.composition
        _composition(composition, effective, library, f"{alt_field}.composition")


def _composition(composition: str, choices: Selections, library: FiveLibraries, field: str) -> None:
    index = object_index(library)
    refs = parse_references(composition, index, field=field)
    selected = {mechanism for mechanisms in choices.values() for mechanism in mechanisms}
    if any(index[ref] == "mechanisms" and ref not in selected for ref in refs):
        fail(field, composition, "组织说明引用了未采用机制; 请提供有效 composition")


def _consumers(library: FiveLibraries, index: Mapping[str, str]) -> None:
    for item in (*library.features, *library.relations):
        field = f"{index[item.id]}.{item.id}.mechanism_refs"
        _unique(tuple(ref.id for ref in item.mechanism_refs), field)
        for ref in item.mechanism_refs:
            _ref(ref.id, {"mechanisms"}, index, field)
    for relation in library.relations:
        field = f"relations.{relation.id}.participants"
        _unique(tuple(part.ref for part in relation.participants), field)
        for part in relation.participants:
            _ref(part.ref, {"elements", "features"}, index, field)


def _mechanisms(library: FiveLibraries, index: Mapping[str, str]) -> None:
    for mechanism in library.mechanisms:
        for number, text in enumerate((mechanism.method, *(mechanism.conditions or ()))):
            field = f"mechanisms.{mechanism.id}.method_or_conditions[{number}]"
            refs = parse_references(text, index, field=field)
            if any(index[ref] != "mechanisms" for ref in refs):
                fail(field, text, "可复用机制不能绑定本图消费者")


def validate_libraries(library: FiveLibraries, target_element_id: str) -> None:
    """校验完整快照的身份、候选关联、草图选择和正文.

    Args:
        library: 待发布五库.
        target_element_id: 本轮固定目标.

    Raises:
        LibraryValidationError: 引用或结构不符合字段契约.
    """
    index = object_index(library)
    _ref(target_element_id, {"elements"}, index, "target_element_id")
    for element in library.elements:
        _unique(element.feature_ids, f"elements.{element.id}.feature_ids")
        for ref in element.feature_ids:
            _ref(ref, {"features"}, index, f"elements.{element.id}.feature_ids")
    _consumers(library, index)
    _mechanisms(library, index)
    for kind in KINDS:
        for item in getattr(library, kind):
            _model_texts(item, index, f"{kind}.{item.id}")
    for sketch in library.sketches:
        _sketch(sketch, library, target_element_id)


def exploration_library(submission: ExplorationSubmission, target: Element) -> FiveLibraries:
    """将来源观察绑定到目标, 不采用主 agent 的特征草稿."""
    element = target.model_copy(update={"feature_ids": tuple(feature.id for feature in submission.features)})
    return FiveLibraries(
        elements=(element,),
        features=submission.features,
        relations=submission.relations,
        mechanisms=submission.mechanisms,
        sketches=submission.sketches,
    )


def validate_exploration(submission: ExplorationSubmission, target: Element) -> None:
    """校验单来源产物及问题引用, 不规定草图数量.

    Args:
        submission: 当前 worker 的完整提交.
        target: 固定元素与范围.

    Raises:
        LibraryValidationError: 来源内引用非法或必要缺口未记录.
    """
    library = exploration_library(submission, target)
    validate_libraries(library, target.id)
    index = object_index(library)
    for issue in (*submission.open_questions, *submission.gaps):
        _unique(issue.refs, "issues.refs")
        for ref in issue.refs:
            _ref(ref, set(KINDS), index, "issues.refs")
        _model_texts(issue, index, "issues")
    _missing_candidates(submission)
    _missing_sketch_targets(submission)


def _missing_candidates(submission: ExplorationSubmission) -> None:
    if not submission.features and not submission.gaps:
        fail("features", submission.features, "无观察特征必须保留缺口")
    for item in (*submission.features, *submission.relations):
        covered = any(not issue.refs or item.id in issue.refs for issue in submission.gaps)
        if not item.mechanism_refs and not covered:
            fail(f"{item.id}.mechanism_refs", (), "候选机制为空必须记录对应缺口")


def _missing_sketch_targets(submission: ExplorationSubmission) -> None:
    # 草图只承担自身来源观察的覆盖责任, 不把兄弟报告的新观察强加给原方案.
    observed = {item.id for item in (*submission.features, *submission.relations)}
    for sketch in submission.sketches:
        missing = observed - sketch.default.keys()
        unrecorded = {
            identity
            for identity in missing
            if not any(not issue.refs or identity in issue.refs or sketch.id in issue.refs for issue in submission.gaps)
        }
        if unrecorded:
            fail(f"sketches.{sketch.id}.default", sorted(unrecorded), "草图未覆盖本来源观察目标; 必须记录对应 gaps")
