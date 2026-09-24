"""初稿与四库的引用、范围和依赖一致性校验."""

from __future__ import annotations

from typing import NoReturn

from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.library.models import (
    Choice,
    Element,
    ElementScope,
    ExplorationReport,
    Feature,
    FeatureChoice,
    FinalFeatureChoice,
    FinalSketch,
    Library,
    Participant,
    PossibilityLibrary,
    Relation,
    RelationChoice,
    Sketch,
    VisualOutline,
)


def _fail(path: str, code: str, message: str) -> NoReturn:
    """以局部修复可使用的 JSON 路径抛出首个结构错误."""
    raise AnalysisValidationError([{"path": path, "code": code, "message": message}])


def _unique(values: tuple[str, ...], path: str) -> None:
    """同一引用列表不能重复同一身份."""
    if len(values) != len(set(values)):
        _fail(path, "duplicate_reference", "同一列表不能重复引用同一 ID")


def _elements(elements: tuple[Element, ...]) -> dict[str, Element]:
    """先检查统一元素和实例组身份, 再建立引用表."""
    seen: set[str] = set()
    for index, element in enumerate(elements):
        identities = [(element.id, f"/elements/{index}/id")]
        identities.extend((group.id, f"/elements/{index}/instance_groups/{offset}/id") for offset, group in enumerate(element.instance_groups))
        for identity, path in identities:
            if identity in seen:
                _fail(path, "duplicate_id", "元素与实例组 ID 必须唯一")
            seen.add(identity)
    return {element.id: element for element in elements}


def _scope(scope: ElementScope, elements: dict[str, Element], path: str) -> None:
    """检查组身份归属于指定元素."""
    if scope.element_id not in elements:
        _fail(path + "/element_id", "unknown_element", f"不存在元素 {scope.element_id}")
    groups = {group.id for group in elements[scope.element_id].instance_groups}
    if scope.group_id is not None and scope.group_id not in groups:
        _fail(path + "/group_id", "invalid_group", "实例组不属于指定元素")


def validate_outline(outline: VisualOutline) -> None:
    """校验统一初稿的身份和可见关系引用.

    Args:
        outline: 已通过字段类型校验的初稿.

    Raises:
        AnalysisValidationError: 元素身份重复或关系引用不合法.
    """
    elements = _elements(outline.elements)
    for index, relation in enumerate(outline.relations):
        seen: set[tuple[str, str | None]] = set()
        for offset, participant in enumerate(relation.participants):
            path = f"/relations/{index}/participants/{offset}"
            _scope(participant, elements, path)
            key = (participant.element_id, participant.group_id)
            if key in seen:
                _fail(path, "duplicate_participant", "同一关系不能重复相同端点")
            seen.add(key)


class _LibraryValidator:
    """分阶段检查身份、引用和草图局部前提, 不求解全部组合."""

    def __init__(self, library: Library, elements: tuple[Element, ...]) -> None:
        self.library = library
        self.elements = _elements(elements)
        self.features = {item.id: item for item in library.feature_library}
        self.relations = {item.id: item for item in library.relation_library}

    def validate(self) -> None:
        """身份错误时停止, 避免继续解释歧义对象."""
        self._identities()
        for index, feature in enumerate(self.library.feature_library):
            self._element_ids(feature.element_ids, f"/feature_library/{index}/element_ids")
        for index, relation in enumerate(self.library.relation_library):
            self._participants(relation, f"/relation_library/{index}")
        self._requirements()
        for index, sketch in enumerate(self.library.sketch_library):
            self._sketch(sketch, f"/sketch_library/{index}")

    def _identities(self) -> None:
        seen: set[str] = set()
        for name in ("sketch_library", "feature_library", "relation_library"):
            for index, item in enumerate(getattr(self.library, name)):
                pairs = [(item.id, f"/{name}/{index}/id")]
                if isinstance(item, (Feature, Relation)):
                    pairs.extend((candidate.id, f"/{name}/{index}/candidates/{offset}/id") for offset, candidate in enumerate(item.candidates))
                for identity, path in pairs:
                    if identity in seen:
                        _fail(path, "duplicate_id", "报告中的草图、特征、关系和候选 ID 必须唯一")
                    seen.add(identity)

    def _element_ids(self, identities: tuple[str, ...], path: str) -> None:
        _unique(identities, path)
        for index, identity in enumerate(identities):
            if identity not in self.elements:
                _fail(f"{path}/{index}", "unknown_element", f"不存在元素 {identity}")

    def _owner(self, kind: str, identity: str, path: str) -> Feature | Relation:
        item = self.features.get(identity) if kind == "feature" else self.relations.get(identity)
        if item is None:
            _fail(path, "unknown_reference", f"不存在 {kind} {identity}")
        return item

    def _candidates(self, kind: str, identity: str, candidates: tuple[str, ...], path: str) -> None:
        owner = self._owner(kind, identity, path)
        _unique(candidates, path)
        allowed = {item.id for item in owner.candidates}
        if set(candidates) - allowed:
            _fail(path, "invalid_candidate_owner", "候选不属于被引用的特征或关系")

    def _participants(self, relation: Relation, path: str) -> None:
        seen: set[tuple[str, str, str | None]] = set()
        for index, participant in enumerate(relation.participants):
            position = f"{path}/participants/{index}"
            key = (participant.kind, participant.id, participant.group_id)
            if key in seen:
                _fail(position, "duplicate_participant", "端点不能重复或同时担任两个方向")
            seen.add(key)
            self._participant(participant, position)
        roles = {participant.role for participant in relation.participants}
        expected = {"source", "target"} if relation.kind == "dependency" else {None}
        if roles != expected:
            _fail(path + "/participants", "invalid_direction", "依赖关系每个端点须有方向且同时包含源和目标; 组织关系不能携带方向")

    def _participant(self, participant: Participant, path: str) -> None:
        if participant.kind == "element":
            _scope(ElementScope(element_id=participant.id, group_id=participant.group_id), self.elements, path)
        else:
            self._owner(participant.kind, participant.id, path + "/id")
            if participant.group_id is not None:
                _fail(path + "/group_id", "invalid_group", "实例组只能附在元素端点上")

    def _requirements(self) -> None:
        for name in ("feature_library", "relation_library"):
            for index, item in enumerate(getattr(self.library, name)):
                for offset, candidate in enumerate(item.candidates):
                    for position, requirement in enumerate(candidate.requires):
                        path = f"/{name}/{index}/candidates/{offset}/requires/{position}"
                        self._candidates(requirement.kind, requirement.id, requirement.candidate_ids, path)

    def _item_scopes(self, kind: str, identity: str, visited: set[str] | None = None) -> set[tuple[str, str | None]]:
        if kind == "feature":
            return {(element, None) for element in self.features[identity].element_ids}
        visited = set() if visited is None else visited
        if identity in visited:
            return set()
        visited.add(identity)
        scopes: set[tuple[str, str | None]] = set()
        for participant in self.relations[identity].participants:
            if participant.kind == "element":
                scopes.add((participant.id, participant.group_id))
            else:
                scopes.update(self._item_scopes(participant.kind, participant.id, visited))
        return scopes

    def _choice_scope(self, choice: Choice, kind: str, identity: str, sketch: Sketch | FinalSketch, path: str) -> set[tuple[str, str | None]]:
        allowed = self._item_scopes(kind, identity)
        scopes = allowed if choice.applies_to is None else {(item.element_id, item.group_id) for item in choice.applies_to}
        if choice.applies_to is not None:
            if len(scopes) != len(choice.applies_to):
                _fail(path + "/applies_to", "duplicate_scope", "适用范围不能重复")
            if any(group is not None and (element, None) in scopes for element, group in scopes):
                _fail(path + "/applies_to", "overlapping_scope", "同一次声明不能同时包含整元素和它的实例组")
            for index, scope in enumerate(choice.applies_to):
                _scope(scope, self.elements, f"{path}/applies_to/{index}")
                if (scope.element_id, scope.group_id) not in allowed and (scope.element_id, None) not in allowed:
                    _fail(path + "/applies_to", "invalid_scope", "适用范围只能收窄被引用条目的范围")
        if {element for element, _ in scopes} - set(sketch.element_ids):
            _fail(path, "outside_sketch", "引用范围包含草图之外的元素")
        return scopes

    def _sketch(self, sketch: Sketch | FinalSketch, path: str) -> None:
        self._element_ids(sketch.element_ids, path + "/element_ids")
        selected: dict[tuple[str, str], set[str]] = {}
        scopes: dict[tuple[str, str], list[set[tuple[str, str | None]]]] = {}
        for kind, choices in (("feature", sketch.feature_refs), ("relation", sketch.relation_refs)):
            for index, choice in enumerate(choices):
                position = f"{path}/{kind}_refs/{index}"
                identity = choice.feature_id if isinstance(choice, (FeatureChoice, FinalFeatureChoice)) else choice.relation_id
                candidates = (
                    choice.candidate_ids
                    if isinstance(choice, (FeatureChoice, RelationChoice))
                    else tuple(item.candidate_id for item in choice.candidate_refs)
                )
                self._candidates(kind, identity, candidates, position)
                scope = self._choice_scope(choice, kind, identity, sketch, position)
                key = (kind, identity)
                if any(_overlap(scope, previous) for previous in scopes.get(key, [])):
                    _fail(position, "overlapping_choice", "同一条目的多次引用须使用互不重叠的实例组范围")
                scopes.setdefault(key, []).append(scope)
                selected.setdefault(key, set()).update(candidates)
        self._sketch_dependencies(sketch, selected, path)

    def _sketch_dependencies(self, sketch: Sketch | FinalSketch, selected: dict[tuple[str, str], set[str]], path: str) -> None:
        for (kind, identity), candidates in selected.items():
            owner = self._owner(kind, identity, path)
            if isinstance(owner, Relation):
                for participant in owner.participants:
                    exists = participant.id in sketch.element_ids if participant.kind == "element" else (participant.kind, participant.id) in selected
                    if not exists:
                        _fail(path + "/relation_refs", "missing_endpoint", "引用关系的全部端点须处于草图相关引用范围内")
            for candidate in owner.candidates:
                if candidate.id not in candidates:
                    continue
                for requirement in candidate.requires:
                    if not set(requirement.candidate_ids) & selected.get((requirement.kind, requirement.id), set()):
                        _fail(path, "missing_requirement", f"候选 {candidate.id} 的直接前提 {requirement.id} 不在草图保留范围内")


def _overlap(left: set[tuple[str, str | None]], right: set[tuple[str, str | None]]) -> bool:
    """整元素范围与任一所属组重叠, 不将不同实例组混为同一范围."""
    return not left or not right or any(a == b and (ga is None or gb is None or ga == gb) for a, ga in left for b, gb in right)


def validate_exploration(report: ExplorationReport, outline: VisualOutline) -> None:
    """校验独立报告对统一元素、候选和直接前提的引用.

    Args:
        report: 子任务的三库报告.
        outline: 本次探索绑定的统一视觉初稿.

    Raises:
        AnalysisValidationError: 报告为空或业务引用不合法.
    """
    validate_outline(outline)
    if not (report.sketch_library or report.feature_library or report.relation_library):
        _fail("/", "empty_exploration", "无法形成候选时应报告未完成原因, 不能提交空探索")
    _LibraryValidator(report, outline.elements).validate()


def validate_library(library: PossibilityLibrary) -> None:
    """校验最终四库, 允许元素保留尚未解释的问题.

    Args:
        library: 后端组装的最终可能性库.

    Raises:
        AnalysisValidationError: 身份、范围、方向或直接前提不合法.
    """
    _LibraryValidator(library, library.elements).validate()
