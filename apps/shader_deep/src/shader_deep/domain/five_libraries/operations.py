"""机械汇集与受限合并; 草图只更新引用, 保留原有组织."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.five_libraries.models import (
    Element,
    ExplorationSubmission,
    Feature,
    FiveLibraries,
    Issue,
    MechanismRef,
    MergeProposal,
    Relation,
)
from shader_deep.domain.five_libraries.references import fail, resolve_mapping, rewrite_text
from shader_deep.domain.five_libraries.validation import (
    KINDS,
    TEXT_FIELDS,
    exploration_library,
    object_index,
    validate_exploration,
    validate_libraries,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

REFERENCE_FIELDS = {"id", "ref", "element_id", "feature_ids", "refs"}


def _selection(value: dict[str, object], mapping: Mapping[str, str], field: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for target, mechanisms in value.items():
        mapped_target = mapping.get(target, target)
        if mapped_target in result:
            fail(field, target, "选择目标合并后撞键; 不允许联合或丢弃原选择")
        if not isinstance(mechanisms, (list, tuple)):
            fail(field, mechanisms, "机制选择必须为数组")
        mapped = [mapping.get(str(item), str(item)) for item in mechanisms]
        if len(mapped) != len(set(mapped)):
            fail(field, mechanisms, "共同采用的机制合并后塌缩; 无法机械证明方案保义")
        result[mapped_target] = mapped
    return result


def _rewrite(value: object, mapping: Mapping[str, str], key: str = "", field: str = "") -> object:
    if isinstance(value, str):
        return mapping.get(value, value) if key in REFERENCE_FIELDS else rewrite_text(value, mapping) if key in TEXT_FIELDS else value
    if isinstance(value, (tuple, list)):
        return [_rewrite(child, mapping, key, f"{field}[{number}]") for number, child in enumerate(value)]
    if isinstance(value, dict):
        if key in {"default", "choices"}:
            return _selection(value, mapping, field)
        return {child_key: _rewrite(child, mapping, str(child_key), f"{field}.{child_key}") for child_key, child in value.items()}
    return value


def rewrite_issues(issues: tuple[Issue, ...], mapping: Mapping[str, str]) -> tuple[Issue, ...]:
    """同步运行问题引用, 合并后重复 refs 只保留一次.

    Args:
        issues: 原始未知项或缺口.
        mapping: 已接受的直接 ID 映射.

    Returns:
        保留问题说明和非引用正文的更新结果.
    """
    return tuple(
        Issue(refs=tuple(dict.fromkeys(mapping.get(ref, ref) for ref in issue.refs)), description=rewrite_text(issue.description, mapping))
        for issue in issues
    )


def _namespace(library: FiveLibraries, counts: dict[str, int]) -> dict[str, str]:
    prefixes = {"features": "F", "relations": "R", "mechanisms": "M", "sketches": "S"}
    mapping = {element.id: element.id for element in library.elements}
    for kind, prefix in prefixes.items():
        for item in getattr(library, kind):
            counts[kind] += 1
            candidate = f"{prefix}{counts[kind]}"
            while candidate in mapping.values():
                counts[kind] += 1
                candidate = f"{prefix}{counts[kind]}"
            mapping[item.id] = candidate
    return mapping


def exploration_mappings(target: Element, reports: dict[str, ExplorationSubmission]) -> dict[str, dict[str, str]]:
    """生成运行记录所需的来源本地 ID 到包内 ID 对应.

    Args:
        target: 本轮固定目标元素.
        reports: 按稳定任务顺序排列的来源产物.

    Returns:
        每个来源的完整直接映射, 包含目标元素身份.

    Raises:
        LibraryValidationError: 任一来源产物不符合提交契约.
    """
    counts = dict.fromkeys(KINDS, 0)
    mappings: dict[str, dict[str, str]] = {}
    for source, report in reports.items():
        validate_exploration(report, target)
        mappings[source] = _namespace(exploration_library(report, target), counts)
    return mappings


def collect_explorations(target: Element, reports: dict[str, ExplorationSubmission]) -> tuple[FiveLibraries, tuple[Issue, ...], tuple[Issue, ...]]:
    """按来源一次规范化 ID 并完整汇集, 不去重草图.

    Args:
        target: 本轮固定元素.
        reports: 按稳定任务顺序排列的合法来源产物.

    Returns:
        未去重五库基线、未知项与业务缺口.

    Raises:
        LibraryValidationError: 来源非法或无法形成可用观察基线.
    """
    collected: list[FiveLibraries] = []
    questions: list[Issue] = []
    gaps: list[Issue] = []
    mappings = exploration_mappings(target, reports)
    for source, report in reports.items():
        original = exploration_library(report, target)
        mapping = mappings[source]
        collected.append(FiveLibraries.model_validate(_rewrite(original.model_dump(), mapping)))
        questions.extend(rewrite_issues(report.open_questions, mapping))
        gaps.extend(rewrite_issues(report.gaps, mapping))
    return _collected_library(target, collected), tuple(questions), tuple(gaps)


def _collected_library(target: Element, sources: list[FiveLibraries]) -> FiveLibraries:
    features = tuple(item for source in sources for item in source.features)
    if not features:
        fail("reports", (), "无法形成含观察特征的合法可用基线")
    element = target.model_copy(update={"feature_ids": tuple(item.id for item in features)})
    result = FiveLibraries(
        elements=(element,),
        features=features,
        relations=tuple(item for source in sources for item in source.relations),
        mechanisms=tuple(item for source in sources for item in source.mechanisms),
        sketches=tuple(item for source in sources for item in source.sketches),
    )
    validate_libraries(result, target.id)
    return result


def merge_mapping(library: FiveLibraries, proposal: MergeProposal) -> dict[str, str]:
    """验证合并组身份, 产生供业务问题同步使用的直接映射.

    Args:
        library: 原始完整快照.
        proposal: 仅 F/R/M 的合并建议.

    Returns:
        被删除项到保留项的直接映射.

    Raises:
        LibraryValidationError: 类型、组成员或重叠组非法.
    """
    index = object_index(library)
    mapping: dict[str, str] = {}
    grouped: set[str] = set()
    for group in proposal.groups:
        if group.keep not in group.ids or len(set(group.ids)) != len(group.ids):
            fail("groups", group.ids, "keep 必须属于无重复的合并组")
        if grouped.intersection(group.ids):
            fail("groups", group.ids, "合并组不能重叠或形成链; 一次明确最终保留项")
        if any(index.get(ref) != group.kind for ref in group.ids):
            fail("groups", group.ids, "合并对象不存在或跨类型")
        grouped.update(group.ids)
        mapping.update({ref: group.keep for ref in group.ids if ref != group.keep})
    return resolve_mapping(index, mapping)


def _candidate_union(items: tuple[Feature, ...] | tuple[Relation, ...], ids: tuple[str, ...]) -> tuple[MechanismRef, ...]:
    refs: dict[str, MechanismRef] = {}
    for item in items:
        if item.id in ids:
            for ref in item.mechanism_refs:
                refs.setdefault(ref.id, ref)
    return tuple(refs.values())


def _retained(library: FiveLibraries, proposal: MergeProposal, mapping: Mapping[str, str]) -> FiveLibraries:
    data = library.model_dump()
    for kind in KINDS:
        retained = []
        for item in getattr(library, kind):
            if item.id in mapping:
                continue
            group = next((group for group in proposal.groups if group.keep == item.id), None)
            selected = item
            if group and isinstance(item, (Feature, Relation)):
                items = library.features if isinstance(item, Feature) else library.relations
                selected = item.model_copy(update={"mechanism_refs": _candidate_union(items, group.ids)})
            retained.append(selected.model_dump())
        data[kind] = retained
    return FiveLibraries.model_validate(data)


def _merge_conflicts(library: FiveLibraries, proposal: MergeProposal, mapping: Mapping[str, str]) -> None:
    for relation in library.relations:
        endpoints = [mapping.get(part.ref, part.ref) for part in relation.participants]
        if len(endpoints) != len(set(endpoints)):
            fail(f"relations.{relation.id}.participants", endpoints, "合并导致关系端点塌缩")
    relations = {item.id: item for item in library.relations}
    for group in proposal.groups:
        if group.kind == "relations":
            signatures = {frozenset((mapping.get(part.ref, part.ref), part.role) for part in relations[ref].participants) for ref in group.ids}
            if len(signatures) != 1:
                fail("groups", group.ids, "关系参与对象或作用不同; 无法机械证明合并保义")


def _deduplicate_candidates(library: FiveLibraries) -> FiveLibraries:
    features = tuple(
        item.model_copy(update={"mechanism_refs": tuple({ref.id: ref for ref in item.mechanism_refs}.values())}) for item in library.features
    )
    relations = tuple(
        item.model_copy(update={"mechanism_refs": tuple({ref.id: ref for ref in item.mechanism_refs}.values())}) for item in library.relations
    )
    elements = tuple(item.model_copy(update={"feature_ids": tuple(dict.fromkeys(item.feature_ids))}) for item in library.elements)
    return library.model_copy(update={"features": features, "relations": relations, "elements": elements})


def apply_merges(library: FiveLibraries, proposal: MergeProposal, target_element_id: str) -> FiveLibraries:
    """整组合并已有 F/R/M, 冲突时保留调用方原快照.

    Args:
        library: 不可变原始快照.
        proposal: 无新对象或草图修改的合并建议.
        target_element_id: 本轮固定元素.

    Returns:
        全部校验通过的新快照.

    Raises:
        LibraryValidationError: 引用冲突、端点塌缩或草图选择改义.
    """
    validate_libraries(library, target_element_id)
    mapping = merge_mapping(library, proposal)
    _merge_conflicts(library, proposal, mapping)
    retained = _retained(library, proposal, mapping)
    rewritten = FiveLibraries.model_validate(_rewrite(retained.model_dump(), mapping))
    result = _deduplicate_candidates(rewritten)
    validate_libraries(result, target_element_id)
    return result
