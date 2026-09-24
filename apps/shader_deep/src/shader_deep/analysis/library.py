"""不可变探索报告、可能性库增量整合及实际材料呈现登记."""

from __future__ import annotations

import hashlib
import json
import re
from copy import copy, deepcopy
from dataclasses import asdict
from threading import RLock
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from langchain.messages import ToolMessage
from pydantic import TypeAdapter, ValidationError

from shader_deep.analysis.exploration import (
    ExplorationReport,
    FinalSketch,
    PossibilityLibrary,
    VisualOutline,
    validate_exploration,
    validate_library,
    validate_outline,
)
from shader_deep.analysis.schemas import AnalysisValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.messages import BaseMessage

    from shader_deep.analysis.integration import IntegrationDecision

Kind = Literal["sketch", "feature", "relation", "candidate"]
Document = dict[str, object]
SECTIONS = {"sketch": "sketch_library", "feature": "feature_library", "relation": "relation_library"}
MAX_LIBRARY_RESPONSE_CHARS = 76_000
MIN_MERGE_SOURCES = 2


def _integration_error(path: str, message: str) -> NoReturn:
    """工具修复路径使用决定自身的顶层字段."""
    raise AnalysisValidationError([{"path": path, "code": "invalid_integration", "message": message}])


def _submission_path(path: str, source_prefix: str, offsets: dict[str, int] | None) -> str:
    """将整库校验位置映射到本批提交, 不向模型暴露不存在的修复路径."""
    if source_prefix and (path == source_prefix or path.startswith(source_prefix + "/")):
        return path[len(source_prefix) :]
    match = re.fullmatch(r"/([^/]+)/(\d+)(/.*)?", path)
    if offsets and match and match[1] in offsets:
        local_index = int(match[2]) - offsets[match[1]]
        if local_index >= 0:
            return f"/{match[1]}/{local_index}{match[3] or ''}"
    return path


def _integration_failure(path: str, error: ValueError, *, source_prefix: str = "", offsets: dict[str, int] | None = None) -> NoReturn:
    """保留嵌套业务或结构错误的精确字段路径, 便于局部修复."""
    if isinstance(error, AnalysisValidationError):
        issues = [{**issue, "path": path + _submission_path(str(issue["path"]), source_prefix, offsets)} for issue in error.issues]
        raise AnalysisValidationError(issues) from error
    if isinstance(error, ValidationError):
        issues: list[dict[str, object]] = [
            {
                "path": path + _submission_path("/" + "/".join(str(part) for part in issue["loc"]), source_prefix, offsets),
                "code": issue["type"],
                "message": issue["msg"],
            }
            for issue in error.errors()
        ]
        raise AnalysisValidationError(issues) from error
    _integration_error(path, str(error))


def _merge_failure(path: str, error: ValueError) -> NoReturn:
    """合并衍生的整库错误定位成员数组, 影响位置只作为解释保留."""
    try:
        _integration_failure("", error)
    except AnalysisValidationError as failure:
        issues = [
            {
                **issue,
                "path": path,
                "message": str(issue["message"]) + (f"; affected library path: {issue['path']}" if issue["path"] else ""),
            }
            for issue in failure.issues
        ]
        raise AnalysisValidationError(issues) from error


def _document(value: object) -> Document:
    """只接受对象形状, 便于对已校验结构做机械变换."""
    if not isinstance(value, dict):
        msg = "Expected an object"
        raise TypeError(msg)
    return cast("Document", value)


def _objects(value: object) -> list[Document]:
    """将已校验的数组投影为对象列表."""
    return [_document(item) for item in cast("list[object]", value)]


def _compact(value: object) -> object:
    """业务读取省略空的可选字段, 完整持久化副本仍保留默认值."""
    if isinstance(value, dict):
        return {key: _compact(child) for key, child in value.items() if child not in (None, [], {})}
    if isinstance(value, list):
        return [_compact(child) for child in value]
    return value


def _dump(value: object) -> str:
    """使用同一序列化方式验证完整工具正文."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _index(data: Document) -> dict[str, tuple[str, Document, str | None]]:
    """建立拥有者与候选索引, 候选身份在整个库中唯一."""
    entries: dict[str, tuple[str, Document, str | None]] = {}
    for kind, section in SECTIONS.items():
        for item in _objects(data[section]):
            identifier = str(item["id"])
            entries[identifier] = (kind, item, None)
            for candidate in _objects(item.get("candidates", [])):
                entries[str(candidate["id"])] = ("candidate", candidate, identifier)
    return entries


def _rewrite(value: object, mapping: dict[str, str], *, parent_kind: str = "") -> object:
    """只重写业务身份字段, 不改自由文本或统一元素身份."""
    if isinstance(value, list):
        return [_rewrite(item, mapping, parent_kind=parent_kind) for item in value]
    if not isinstance(value, dict):
        return value
    kind = str(value.get("kind", parent_kind))
    result: Document = {}
    for key, child in value.items():
        if key in {"feature_id", "relation_id", "candidate_id"} or (key == "id" and kind != "element"):
            result[key] = mapping.get(str(child), child)
        elif key == "candidate_ids":
            result[key] = list(dict.fromkeys(mapping.get(str(item), str(item)) for item in child))
        else:
            result[key] = _rewrite(child, mapping, parent_kind=kind)
    return result


def _final_report(report: ExplorationReport, namespace: str) -> Document:
    """隔离局部身份并将无排序的探索引用转换为最终引用."""
    data = _document(json.loads(_dump(asdict(report))))
    mapping = {identifier: f"{namespace}::{identifier}" for identifier in _index(data)}
    data = _document(_rewrite(data, mapping))
    for sketch in _objects(data["sketch_library"]):
        for section in ("feature_refs", "relation_refs"):
            for reference in _objects(sketch.get(section, [])):
                reference["candidate_refs"] = [{"candidate_id": identifier} for identifier in cast("list[str]", reference.pop("candidate_ids"))]
    return data


def _dependencies(entry: tuple[str, Document, str | None]) -> list[str]:
    """关系端点和选择前提分别取边, 不对环作真假判断."""
    kind, item, _ = entry
    identifiers = [str(candidate["id"]) for candidate in _objects(item.get("candidates", []))]
    for section, key in (("feature_refs", "feature_id"), ("relation_refs", "relation_id")):
        identifiers.extend(str(reference[key]) for reference in _objects(item.get(section, [])))
    for requirement in _objects(item.get("requires", [])):
        identifiers.append(str(requirement["id"]))
        identifiers.extend(str(identifier) for identifier in cast("list[str]", requirement["candidate_ids"]))
    if kind == "relation":
        identifiers.extend(str(participant["id"]) for participant in _objects(item["participants"]) if participant["kind"] != "element")
    return identifiers


def _read_edges(kind: str, item: Document, *, all_candidates: bool) -> list[tuple[str, bool]]:
    """返回正文读取关系, 拥有者元数据与备选候选集合分别展开."""
    edges: list[tuple[str, bool]] = []
    if all_candidates:
        edges.extend((str(candidate["id"]), True) for candidate in _objects(item.get("candidates", [])))
    for section, key in (("feature_refs", "feature_id"), ("relation_refs", "relation_id")):
        for reference in _objects(item.get(section, [])):
            edges.append((str(reference[key]), False))
            edges.extend((str(candidate["candidate_id"]), True) for candidate in _objects(reference["candidate_refs"]))
    for requirement in _objects(item.get("requires", [])):
        edges.append((str(requirement["id"]), False))
        edges.extend((identifier, True) for identifier in cast("list[str]", requirement["candidate_ids"]))
    if kind == "relation":
        edges.extend((str(participant["id"]), False) for participant in _objects(item["participants"]) if participant["kind"] != "element")
    return edges


class LibraryStore:
    """管理完整四库, 仅允许显式且可追溯的语义整合."""

    def __init__(self, outline: VisualOutline, directory: Path, *, max_response_chars: int = MAX_LIBRARY_RESPONSE_CHARS) -> None:
        """绑定已校验初稿, 创建运行内的版本制品目录."""
        validate_outline(outline)
        self.outline = outline
        self.directory = directory / "possibility_library"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_response_chars = max_response_chars
        self.revision = 0
        self.presented: set[str] = set()
        self._presented_versions: dict[str, str] = {}
        self.provenance: dict[str, list[Document]] = {}
        self.aliases: dict[str, str] = {}
        self._reports: dict[str, ExplorationReport] = {}
        self._report_receipts: dict[str, list[str]] = {}
        self._integration_receipts: dict[str, Document] = {}
        self._progress: dict[str, Document] = {}
        self._lock = RLock()
        self._staging = False
        self._pending: dict[str, tuple[str, dict[str, str]]] = {}
        self._add_counter = 0
        self._continuations: dict[str, list[str]] = {}
        self._fragments: dict[str, Document] = {}
        self._fragment_targets: dict[str, tuple[str, str]] = {}
        self._fragment_groups: dict[tuple[str, str], set[str]] = {}
        self._presented_fragments: set[str] = set()
        elements = [{key: value for key, value in asdict(element).items() if key != "salient_features"} for element in outline.elements]
        self._library = TypeAdapter(PossibilityLibrary).validate_python({"elements": elements, **{section: [] for section in SECTIONS.values()}})
        validate_library(self._library)

    @property
    def library(self) -> PossibilityLibrary:
        """返回不可变且已完成结构校验的业务对象."""
        return self._library

    @property
    def report_ids(self) -> tuple[str, ...]:
        """返回已纳入工作集合的原报告身份."""
        return tuple(self._reports)

    @property
    def unread_handles(self) -> list[str]:
        """返回尚未完整呈现的当前条目, 包括未引用候选."""
        return [identifier for identifier in self._entries() if identifier not in self.presented]

    def _data(self) -> Document:
        """复制完整持久化形状, 不受模型视图省略空字段影响."""
        return _document(json.loads(_dump(asdict(self._library))))

    def _entries(self) -> dict[str, tuple[str, Document, str | None]]:
        """按当前库重建索引, 不缓存过期对象."""
        return _index(self._data())

    def _metadata(self) -> Document:
        """库内容、原始提交、幂等回执和包进度共享一个发布边界."""
        return {
            "reports": {identifier: asdict(report) for identifier, report in self._reports.items()},
            "report_receipts": deepcopy(self._report_receipts),
            "integration_receipts": deepcopy(self._integration_receipts),
            "integration_progress": deepcopy(self._progress),
        }

    def _load_metadata(self, metadata: Document) -> None:
        """从已发布快照同步内存台账, 不依赖第二次文件写入."""
        self._reports = {key: TypeAdapter(ExplorationReport).validate_python(value) for key, value in _document(metadata["reports"]).items()}
        self._report_receipts = cast("dict[str, list[str]]", deepcopy(metadata["report_receipts"]))
        self._integration_receipts = cast("dict[str, Document]", deepcopy(metadata["integration_receipts"]))
        self._progress = cast("dict[str, Document]", deepcopy(metadata["integration_progress"]))

    def material_entries(self) -> dict[str, Document]:
        """提供完整、不重复的材料正文; 拥有者只列候选身份."""
        entries = self._entries()
        result: dict[str, Document] = {
            element.id: {"handle": element.id, "kind": "element", "content": _compact(asdict(element))} for element in self.library.elements
        }
        for identifier, (_, item, _) in entries.items():
            material = self._read_object(identifier, entries)
            if "candidates" in item:
                material["candidate_ids"] = [str(candidate["id"]) for candidate in _objects(item["candidates"])]
            result[identifier] = material
        return result

    def material_closure(self, identifiers: list[str]) -> list[str]:
        """返回一次判断所需的完整关联身份, 不生成正文分片."""
        return self._closure(identifiers)

    def material_versions(self, identifiers: list[str]) -> dict[str, str]:
        """以完整材料哈希绑定条目正文、拥有者和候选集合."""
        materials = self.material_entries()
        result: dict[str, str] = {}
        for identifier in identifiers:
            if identifier not in materials:
                msg = f"Material no longer exists: {identifier}"
                raise ValueError(msg)
            result[identifier] = hashlib.sha256(_dump(materials[identifier]).encode("utf-8")).hexdigest()
        return result

    def present_materials(self, versions: dict[str, str]) -> None:
        """调用者确认完整材料已进入实际请求后登记, 拒绝过期材料."""
        with self._lock:
            self._check_material_versions(versions)
            self.presented.update(versions)
            self._presented_versions.update(versions)

    def _check_material_versions(self, versions: dict[str, str]) -> None:
        """相关材料过期则整包拒绝, 无关库版本递增不影响本包."""
        if self.material_versions(list(versions)) != versions:
            msg = "Material versions changed; refresh this package before submitting"
            raise ValueError(msg)

    def integration_receipt(self, submission_id: str) -> Document | None:
        """读取已发布的原始回执, 用于重试及运行状态同步."""
        stored = self._integration_receipts.get(submission_id)
        return deepcopy(_document(stored["receipt"])) if stored is not None else None

    def apply_integration(
        self,
        decision: IntegrationDecision,
        *,
        submission_id: str,
        package_id: str,
        editable_ids: list[str],
        material_versions: dict[str, str],
        progress: Document | None = None,
    ) -> Document:
        """在副本应用本包全部决定, 一次发布版本、回执和处理进度.

        Args:
            decision: 主 Agent 的语义决定, 不包含执行器台账.
            submission_id: 执行器绑定的幂等提交身份.
            package_id: 本次判断对应的材料包身份.
            editable_ids: 本包允许语义修改的原始库身份.
            material_versions: 实际完整呈现的判断材料版本.
            progress: 执行器随本次成功发布保存的包进度.

        Returns:
            首次成功发布时生成的回执; 重放返回同一回执.

        Raises:
            ValueError: 材料过期、未呈现、越界、身份冲突或整批校验失败.
        """
        with self._lock:
            fingerprint = _dump({"package_id": package_id, "decision": decision.model_dump(mode="json")})
            previous = self._integration_receipts.get(submission_id)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    msg = "Submission identity already accepted with different content"
                    raise ValueError(msg)
                return deepcopy(_document(previous["receipt"]))
            self._check_material_versions(material_versions)
            self._validate_presented_versions(material_versions)
            self._validate_integration(decision, set(editable_ids), set(material_versions))
            staged = self._stage_integration(decision)
            return self._publish_integration(staged, decision, submission_id, package_id, fingerprint, progress)

    def _validate_presented_versions(self, versions: dict[str, str]) -> None:
        """当前身份曾经呈现不足以证明本次使用的字段版本已经呈现."""
        missing = [
            identifier
            for identifier, version in versions.items()
            if identifier not in self.presented or self._presented_versions.get(identifier) != version
        ]
        if missing:
            _integration_error("/", "Current material versions were not presented in an actual request: " + ", ".join(sorted(missing)))

    def _validate_integration(self, decision: IntegrationDecision, editable: set[str], materials: set[str]) -> None:
        """在修改前固定范围和证据边界, 避免批内机械改写使证明失效."""
        operations = (
            decision.merges,
            decision.reference_updates,
            decision.additions,
            decision.unresolved,
            decision.outline_issue_decisions,
            decision.review_ids,
            decision.deferred_work,
        )
        if decision.preserve and decision.deferred_work:
            _integration_error("/preserve", "preserve and deferred_work are mutually exclusive for one comparison")
        if not decision.preserve and not any(operations):
            _integration_error("/preserve", "Explicitly preserve this package or submit a change")
        if materials - self.presented:
            _integration_error("/", "Complete package materials were not presented in an actual request")
        self._require_materials(list(decision.unresolved), materials, "/unresolved")
        merged = self._validate_merge_materials(decision, editable, materials)
        self._validate_update_materials(decision, editable, materials, merged)
        if decision.additions is not None:
            addition = _index(_final_report(decision.additions, "pending-main"))
            dependencies = [
                identifier
                for kind, item, _ in addition.values()
                for identifier, _ in _read_edges(kind, item, all_candidates=False)
                if identifier in self._entries()
            ]
            self._require_materials(self._judgment_materials(dependencies), materials, "/additions")

    def _validate_merge_materials(self, decision: IntegrationDecision, editable: set[str], materials: set[str]) -> set[str]:
        """拥有者合并不比较其候选; 候选合并只需要拥有者和直接前提."""
        merged: set[str] = set()
        entries = self._entries()
        for index, merge in enumerate(decision.merges):
            path = f"/merges/{index}/members"
            members = list(merge.members)
            if len(set(members)) != len(members) or merged.intersection(members):
                _integration_error(path, "Merge groups must contain distinct, non-overlapping members")
            for position, identifier in enumerate(members):
                if identifier not in editable:
                    _integration_error(f"{path}/{position}", "Merge member is outside this package's editable scope")
                if identifier not in entries or entries[identifier][0] != merge.kind:
                    _integration_error(f"{path}/{position}", "Merge member must exist and have the requested kind")
            merged.update(members)
            required = self._judgment_materials(members) if merge.kind == "candidate" else members
            self._require_materials(required, materials, path)
        return merged

    def _validate_update_materials(self, decision: IntegrationDecision, editable: set[str], materials: set[str], merged: set[str]) -> None:
        """草图修改只需原引用和新引用的完整依据, 不强制读取无关候选."""
        updated: set[str] = set()
        for index, sketch in enumerate(decision.reference_updates):
            path = f"/reference_updates/{index}"
            if sketch.id not in editable or sketch.id in updated:
                _integration_error(path + "/id", "Sketch must be editable and updated at most once")
            if sketch.id in merged:
                _integration_error(path + "/id", "Do not merge and replace the same sketch in one decision")
            updated.add(sketch.id)
            self._require_materials([sketch.id], materials, path)
            original = self._entries()[sketch.id][1]
            replacement = _document(json.loads(_dump(asdict(sketch))))
            changed = self._changed_reference_materials(original, replacement)
            self._require_materials(self._judgment_materials(changed), materials, path)

    @staticmethod
    def _changed_reference_materials(original: Document, replacement: Document) -> list[str]:
        """只读取增删或调整的引用, 无关且未改变的选择不要求重新展开."""
        identifiers: list[str] = []
        for section, key in (("feature_refs", "feature_id"), ("relation_refs", "relation_id")):
            before, after = _objects(original.get(section, [])), _objects(replacement.get(section, []))
            changed = [item for item in before if item not in after] + [item for item in after if item not in before]
            for reference in changed:
                identifiers.append(str(reference[key]))
                identifiers.extend(str(choice["candidate_id"]) for choice in _objects(reference["candidate_refs"]))
        return identifiers

    def comparison_materials(self, identifiers: list[str]) -> list[str]:
        """返回比较目标的最低材料集合, 拥有者不展开候选或引用闭包.

        Args:
            identifiers: 当前比较的正式目标身份.

        Returns:
            目标本身及候选目标的直接判断前提.
        """
        entries = self._entries()
        candidates = [identity for identity in identifiers if identity in entries and entries[identity][2] is not None]
        return sorted(set(identifiers) | set(self._judgment_materials(candidates)))

    def _judgment_materials(self, identifiers: list[str]) -> list[str]:
        """展开本次对象的直接判断依据, 不递归展开前提或拥有者候选."""
        entries = self._entries()
        required = set(identifiers)
        for identifier in identifiers:
            if identifier not in entries:
                continue
            kind, item, owner = entries[identifier]
            if owner is not None:
                required.add(owner)
                owner_kind, owner_item, _ = entries[owner]
                required.update(target for target, _ in _read_edges(owner_kind, owner_item, all_candidates=False))
            required.update(target for target, _ in _read_edges(kind, item, all_candidates=False))
        return sorted(required)

    @staticmethod
    def _require_materials(required: list[str], materials: set[str], path: str) -> None:
        """只检查本次判断必要的正文, 不要求整库逐条已读."""
        missing = set(required) - materials
        if missing:
            raise AnalysisValidationError(
                [
                    {
                        "path": path,
                        "code": "missing_judgment_materials",
                        "message": "Required materials are outside this package; read full bodies: " + ", ".join(sorted(missing)),
                        "missing_ids": sorted(missing),
                        "required_fields": ["body"],
                    }
                ]
            )

    def _stage_integration(self, decision: IntegrationDecision) -> LibraryStore:
        """复用现有业务校验, 副本的中间提交不落盘且不更新呈现边界."""
        staged = copy(self)
        staged._staging = True
        staged.presented = set(self.presented)
        staged._load_metadata(self._metadata())
        ordered = sorted(enumerate(decision.merges), key=lambda pair: {"feature": 0, "relation": 1, "sketch": 2, "candidate": 3}[pair[1].kind])
        for index, merge in ordered:
            try:
                members = sorted(staged.resolve(identifier) for identifier in merge.members)
                staged.merge(merge.kind, members, members[0])
            except ValueError as exc:
                _merge_failure(f"/merges/{index}/members", exc)
        self._stage_updates(staged, decision)
        return staged

    def _staged_failure(self, staged: LibraryStore, decision: IntegrationDecision, error: AnalysisValidationError) -> NoReturn:
        """最终发布校验失败时将跨对象问题归回真实提交位置."""
        issues: list[dict[str, object]] = []
        for issue in error.issues:
            path, affected = self._staged_issue_path(staged, decision, str(issue["path"]))
            issues.append(
                {
                    **issue,
                    "path": path,
                    "affected_ids": [affected] if affected else [],
                    "message": f"{issue['message']}; affected library path: {issue['path']}; affected ID: {affected}",
                }
            )
        raise AnalysisValidationError(issues) from error

    def _staged_issue_path(self, staged: LibraryStore, decision: IntegrationDecision, path: str) -> tuple[str, str]:
        """优先定位显式修复或新增字段, 机械重写冲突定位引发变更的合并."""
        match = re.fullmatch(r"/([^/]+)/(\d+)(/.*)?", path)
        if match is None:
            return ("/merges/0/members" if decision.merges else "/additions", "")
        section, offset, suffix = match[1], int(match[2]), match[3] or ""
        items = _objects(staged._data().get(section, []))
        affected = str(items[offset]["id"]) if offset < len(items) else ""
        for index, sketch in enumerate(decision.reference_updates):
            if sketch.id == affected:
                return f"/reference_updates/{index}{suffix}", affected
        addition_path = self._addition_issue_path(decision, section, affected, suffix)
        if addition_path is not None:
            return addition_path, affected
        if section == "elements" and affected in decision.unresolved:
            return f"/unresolved/{affected}", affected
        dependencies = set(_dependencies(staged._entries()[affected])) if affected in staged._entries() else set()
        for index, merge in enumerate(decision.merges):
            if {staged.resolve(identifier) for identifier in merge.members} & {affected, *dependencies}:
                return f"/merges/{index}/members", affected
        return ("/merges/0/members" if decision.merges else "/additions"), affected

    def _addition_issue_path(self, decision: IntegrationDecision, section: str, identifier: str, suffix: str) -> str | None:
        """新增条目使用提交内局部索引, 不暴露库快照的累计数组偏移."""
        if decision.additions is None or identifier in self._entries() or section not in SECTIONS.values():
            return None
        for index, item in enumerate(getattr(decision.additions, section)):
            if identifier.split("::", 1)[-1] == item.id:
                return f"/additions/{section}/{index}{suffix}"
        return None

    @staticmethod
    def _stage_updates(staged: LibraryStore, decision: IntegrationDecision) -> None:
        """将原包引用机械映射到归并后身份, 其余语义字段原样验证."""
        for index, sketch in enumerate(decision.reference_updates):
            source_index = next((position for position, item in enumerate(staged.library.sketch_library) if item.id == sketch.id), None)
            try:
                replacement = _rewrite(json.loads(_dump(asdict(sketch))), {identifier: staged.resolve(identifier) for identifier in staged.aliases})
                staged._deduplicate_refs({"sketch_library": [replacement]})
                staged.update_sketch(TypeAdapter(FinalSketch).validate_python(replacement))
            except ValueError as exc:
                # 整库校验使用库中的位置, 模型修复必须回到本次提交数组的位置.
                _integration_failure(f"/reference_updates/{index}", exc, source_prefix=f"/sketch_library/{source_index}")
        if decision.additions is not None:
            offsets = {section: len(_objects(staged._data()[section])) for section in SECTIONS.values()}
            try:
                addition = _rewrite(
                    json.loads(_dump(asdict(decision.additions))), {identifier: staged.resolve(identifier) for identifier in staged.aliases}
                )
                staged.add(TypeAdapter(ExplorationReport).validate_python(addition))
            except ValueError as exc:
                _integration_failure("/additions", exc, offsets=offsets)
        if decision.unresolved:
            try:
                staged.update_elements_unresolved({identifier: list(problems) for identifier, problems in decision.unresolved.items()})
            except ValueError as exc:
                _integration_failure("/unresolved", exc)

    def _publish_integration(
        self,
        staged: LibraryStore,
        decision: IntegrationDecision,
        submission_id: str,
        package_id: str,
        fingerprint: str,
        progress: Document | None,
    ) -> Document:
        """将接收结果与变更库一起持久化, 发布失败不改变权威内存."""
        receipt: Document = {
            "status": "accepted",
            "submission_id": submission_id,
            "package_id": package_id,
            "revision": self.revision + 1,
            "preserved": decision.preserve,
            "outline_issue_decisions": [item.model_dump(mode="json") for item in decision.outline_issue_decisions],
            "review_ids": self._review_identifiers(staged, decision.review_ids),
            "deferred_work": list(decision.deferred_work),
        }
        staged._integration_receipts[submission_id] = {"fingerprint": fingerprint, "receipt": receipt}
        staged._progress[package_id] = {"status": "accepted", **(progress or {}), "submission_id": submission_id}
        try:
            self._commit(staged._data(), staged.provenance, staged.aliases, metadata=staged._metadata())
        except AnalysisValidationError as exc:
            self._staged_failure(staged, decision, exc)
        self._add_counter = staged._add_counter
        return deepcopy(receipt)

    def _review_identifiers(self, staged: LibraryStore, requested: tuple[str, ...]) -> list[str]:
        """解析既有身份或本批新增局部身份, 复核只能指向发布后的真实条目."""
        entries = staged.material_entries()
        additions = set(entries) - set(self.material_entries())
        result: list[str] = []
        for index, identifier in enumerate(requested):
            canonical = staged.resolve(identifier)
            if canonical not in entries:
                matches = [item for item in additions if item.split("::", 1)[-1] == identifier]
                if len(matches) != 1:
                    _integration_error(f"/review_ids/{index}", "Review identifier must exist after integration or identify one new local object")
                canonical = matches[0]
            if canonical not in result:
                result.append(canonical)
        return result

    def resolve(self, identifier: str) -> str:
        """将历史句柄解析到规范身份, 合并后旧句柄仍可读取."""
        while identifier in self.aliases:
            identifier = self.aliases[identifier]
        return identifier

    def _commit(
        self,
        data: Document,
        provenance: dict[str, list[Document]],
        aliases: dict[str, str],
        *,
        outline: VisualOutline | None = None,
        metadata: Document | None = None,
    ) -> None:
        """先校验完整副本和写入单一版本制品, 再发布内存状态."""
        library = TypeAdapter(PossibilityLibrary).validate_python(data)
        if not self._staging:
            validate_library(library)
        if self._staging:
            self._library, self.provenance, self.aliases = library, provenance, aliases
            return
        metadata = metadata if metadata is not None else self._metadata()
        revision = self.revision + 1
        snapshot = {
            "revision": revision,
            "outline": asdict(outline or self.outline),
            "library": asdict(library),
            "provenance": provenance,
            "aliases": aliases,
            **metadata,
        }
        destination = self.directory / f"version-{revision:04d}.json"
        if destination.exists():
            msg = "Library version already exists; use a fresh run directory"
            raise ValueError(msg)
        temporary = destination.with_suffix(".tmp")
        try:
            temporary.write_text(_dump(snapshot), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        self._library, self.provenance, self.aliases, self.revision = library, provenance, aliases, revision
        self._load_metadata(metadata)
        versions = self.material_versions(list(self.material_entries()))
        self.presented.intersection_update(
            identifier for identifier, version in versions.items() if self._presented_versions.get(identifier) == version
        )
        self._presented_versions = {identifier: versions[identifier] for identifier in self.presented}

    def add_report(self, report_id: str, report: ExplorationReport) -> list[str]:
        """校验独立原报告并保存不可变副本, 重复同一报告为幂等操作."""
        with self._lock:
            return self._add_report(report_id, report)

    def _add_report(self, report_id: str, report: ExplorationReport) -> list[str]:
        """在短写锁内发布本次探索; 已接受身份始终返回原回执."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", report_id):
            msg = "report_id must contain only letters, digits, underscores, or hyphens"
            raise ValueError(msg)
        if report_id in self._reports:
            if self._reports[report_id] != report:
                msg = f"Immutable report already exists: {report_id}"
                raise ValueError(msg)
            return list(self._report_receipts[report_id])
        validate_exploration(report, self.outline)
        return self._import(report_id, report, author="explorer")

    def _import(self, report_id: str, report: ExplorationReport, *, author: str) -> list[str]:
        """纳入完整并集, 同名但不同机制不触发自动合并."""
        addition = _final_report(report, report_id)
        data, provenance = self._data(), deepcopy(self.provenance)
        identifiers = list(_index(addition))
        if set(identifiers) & (set(self._entries()) | set(self.aliases)):
            msg = "Imported identifiers conflict with existing entries"
            raise ValueError(msg)
        for section in SECTIONS.values():
            cast("list[object]", data[section]).extend(cast("list[object]", addition[section]))
        for identifier in identifiers:
            provenance[identifier] = [{"author": author, "report_id": report_id, "local_id": identifier.split("::", 1)[1]}]
        library = TypeAdapter(PossibilityLibrary).validate_python(data)
        if not self._staging:
            validate_library(library)
        self._save_source(report_id, report)
        metadata = self._metadata()
        _document(metadata["reports"])[report_id] = asdict(report)
        _document(metadata["report_receipts"])[report_id] = identifiers
        self._commit(data, provenance, dict(self.aliases), metadata=metadata)
        if self._staging:
            self._load_metadata(metadata)
        return identifiers

    def _save_source(self, report_id: str, report: ExplorationReport) -> None:
        """使用排他创建保留原报告, 不覆盖上次写入的正文."""
        if self._staging:
            return
        destination = self.directory / f"source-{report_id}.json"
        body = _dump(asdict(report))
        if destination.exists():
            if destination.read_text(encoding="utf-8") == body:
                return
            msg = f"Immutable source differs: {report_id}"
            raise ValueError(msg)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(body)

    def add(self, report: ExplorationReport) -> list[str]:
        """登记主 Agent 新提出的机制, 来源不伪装为子报告."""
        if not (report.sketch_library or report.feature_library or report.relation_library):
            msg = "Main additions must contain at least one object"
            raise ValueError(msg)
        addition = _index(_final_report(report, "pending-main"))
        existing = self._entries()
        dependencies = [identifier for entry in addition.values() for identifier in _dependencies(entry) if identifier in existing]
        self._require_presented(dependencies)
        number = self._add_counter + 1
        while f"main-{number}" in self._reports:
            number += 1
        identifiers = self._import(f"main-{number}", report, author="main")
        self._add_counter = number
        return identifiers

    def catalog(self, offset: int = 0, limit: int = 50) -> Document:
        """分页列出所有对象及候选, 不按优先级隐去任何条目."""
        if offset < 0 or limit < 1:
            msg = "offset must be nonnegative and limit must be positive"
            raise ValueError(msg)
        entries: list[Document] = [
            {
                "handle": identifier,
                "kind": kind,
                "name": str(item.get("name", ""))[:120],
                "description": str(item.get("mechanism", item.get("appearance", item.get("description", item.get("composition", "")))))[:240],
                "owner": owner,
            }
            for identifier, (kind, item, owner) in self._entries().items()
        ]
        page: list[Document] = []
        for entry in entries[offset : offset + limit]:
            if len(_dump(page)) + len(_dump(entry)) + 200 > self.max_response_chars:
                break
            page.append(entry)
        if not page and offset < len(entries):
            msg = "A catalog handle exceeds the response budget; increase max_response_chars"
            raise ValueError(msg)
        end = offset + len(page)
        return {"entries": page, "total": len(entries), "offset": offset, "next_offset": end if end < len(entries) else None}

    def _closure(self, identifiers: list[str]) -> list[str]:
        """草图只展开选中候选; 独立读取拥有者时才展开全部候选."""
        entries = self._entries()
        pending = [(identifier, True) for identifier in identifiers]
        visited: set[tuple[str, bool]] = set()
        result: list[str] = []
        while pending:
            requested, all_candidates = pending.pop(0)
            identifier = self.resolve(requested)
            if (identifier, all_candidates) in visited:
                continue
            if identifier not in entries:
                msg = f"Unknown library handle: {identifier}"
                raise ValueError(msg)
            visited.add((identifier, all_candidates))
            if identifier not in result:
                result.append(identifier)
            kind, item, owner = entries[identifier]
            pending.extend(_read_edges(kind, item, all_candidates=all_candidates))
            if owner is not None:
                pending.append((owner, False))
        return result

    def _read_object(self, identifier: str, entries: dict[str, tuple[str, Document, str | None]] | None = None) -> Document:
        """将候选正文拆成独立完整条目, 拥有者保留完整元数据及候选句柄."""
        kind, item, owner = (entries if entries is not None else self._entries())[identifier]
        content = dict(item)
        candidates = _objects(content.pop("candidates", []))
        result: Document = {"handle": identifier, "kind": kind, "content": _compact(content)}
        if "candidates" in item:
            result["candidate_ids"] = [str(candidate["id"]) for candidate in candidates]
            result["candidate_count"] = len(candidates)
        if owner is not None:
            result["owner"] = owner
        return result

    def _chunks(self, identifier: str, item: Document) -> list[str]:
        """对过大的单个对象编码做无损分段, 正文未完整呈现前不算已读."""
        body = _dump(item)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        key = (identifier, digest)
        if key in self._fragment_groups:
            return sorted(self._fragment_groups[key], key=lambda handle: cast("int", self._fragments[handle]["offset"]))
        handles: list[str] = []
        offset = 0
        while offset < len(body):
            handle = f"chunk-{digest[:20]}-{len(handles)}"
            base: Document = {
                "handle": handle,
                "kind": "json_fragment",
                "source_handle": identifier,
                "sha256": digest,
                "offset": offset,
                "total_chars": len(body),
            }
            low, high = 0, len(body) - offset
            while low < high:
                middle = (low + high + 1) // 2
                if len(_dump({**base, "text": body[offset : offset + middle]})) + 300 <= self.max_response_chars:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                msg = "Response budget cannot hold fragment metadata; increase max_response_chars"
                raise ValueError(msg)
            self._fragments[handle] = {**base, "text": body[offset : offset + low]}
            self._fragment_targets[handle] = key
            handles.append(handle)
            offset += low
        self._fragment_groups[key] = set(handles)
        return handles

    def _materials(self, handles: list[str], entries: dict[str, tuple[str, Document, str | None]]) -> list[str]:
        """普通对象保持完整, 过大对象转为可逐页读取的有限片段序列."""
        result: list[str] = []
        for requested in handles:
            if requested in self._fragments:
                result.append(requested)
                continue
            identifier = self.resolve(requested)
            item = self._read_object(identifier, entries)
            if len(_dump(item)) + 300 > self.max_response_chars:
                result.extend(self._chunks(identifier, item))
            else:
                result.append(identifier)
        return list(dict.fromkeys(result))

    def _material(self, handle: str, entries: dict[str, tuple[str, Document, str | None]]) -> Document:
        """取得请求材料, 片段也是独立合法 JSON 对象."""
        return self._fragments[handle] if handle in self._fragments else self._read_object(handle, entries)

    def read(self, identifier: str, tool_call_id: str | None = None) -> str:
        """读取业务闭包; 过大单项以带摘要和偏移的编码片段无损传输."""
        handles = self._continuations.get(identifier)
        if handles is None:
            handles = [identifier] if identifier in self._fragments else self._closure([identifier])
        entries = self._entries()
        handles = self._materials(handles, entries)
        response: Document = {"objects": [], "pending_handles": [], "oversized_handles": []}
        delivered: dict[str, str] = {}
        deferred: list[str] = []
        for handle in handles:
            item = self._material(handle, entries)
            if len(_dump(response)) + len(_dump(item)) + 200 < self.max_response_chars:
                cast("list[object]", response["objects"]).append(item)
                delivered[handle] = _dump(item)
            else:
                deferred.append(handle)
        response["pending_handles"] = deferred
        fragmented = any(handle in self._fragments for handle in delivered)
        response["complete"] = not deferred and not fragmented
        if fragmented:
            response["fragment_encoding"] = "JSON; concatenate text by source_handle and offset. Source is read after all fragments are presented."
        if deferred:
            continuation = f"read-page-{len(self._continuations) + 1}"
            self._continuations[continuation] = deferred
            response["pending_handles"] = [continuation]
        payload = _dump(response)
        if tool_call_id is not None:
            self._pending[tool_call_id] = (payload, delivered)
        return payload

    def _accept_fragment(self, handle: str, body: str, entries: dict[str, tuple[str, Document, str | None]]) -> None:
        """只有当前对象版本全部片段实际呈现, 才授予该对象已读身份."""
        if body != _dump(self._fragments[handle]):
            return
        identifier, digest = self._fragment_targets[handle]
        if identifier not in entries:
            return
        current = hashlib.sha256(_dump(self._read_object(identifier, entries)).encode("utf-8")).hexdigest()
        if current != digest:
            return
        self._presented_fragments.add(handle)
        if self._fragment_groups[(identifier, digest)] <= self._presented_fragments:
            self.present_materials(self.material_versions([identifier]))

    def on_prepared(self, messages: list[BaseMessage]) -> int:
        """仅登记原调用 ID 下实际进入请求且未被截断的工具正文."""
        before = len(self.presented)
        entries = self._entries()
        for message in messages:
            if not isinstance(message, ToolMessage) or message.tool_call_id not in self._pending:
                continue
            payload, delivered = self._pending[message.tool_call_id]
            if message.status == "error" or message.content != payload:
                continue
            for identifier, body in delivered.items():
                if identifier in self._fragments:
                    self._accept_fragment(identifier, body, entries)
                elif identifier in entries and _dump(self._read_object(identifier, entries)) == body:
                    self.present_materials(self.material_versions([identifier]))
            del self._pending[message.tool_call_id]
        return len(self.presented) - before

    def _require_presented(self, identifiers: list[str]) -> None:
        """语义操作要求正文和完整必要前提均已进入模型请求."""
        if self._staging:
            return
        missing = set(self._closure(identifiers)) - self.presented
        if missing:
            msg = "Read and present related bodies before modifying: " + ", ".join(sorted(missing))
            raise ValueError(msg)

    def merge(self, kind: Kind, source_ids: list[str], target_id: str, *, description: str | None = None) -> Document:
        """合并明确同义的条目, 保留全部不同候选并原子重写关联引用."""
        sources = list(dict.fromkeys(self.resolve(identifier) for identifier in source_ids))
        target = self.resolve(target_id)
        if target not in sources or len(sources) < MIN_MERGE_SOURCES:
            msg = "Merge requires at least two distinct sources including the existing target"
            raise ValueError(msg)
        entries = self._entries()
        if any(identifier not in entries or entries[identifier][0] != kind for identifier in sources):
            msg = "Merge sources must exist and have the requested kind"
            raise ValueError(msg)
        self._require_presented(sources)
        data = self._data()
        if kind == "candidate":
            self._merge_candidates(data, sources, target)
        else:
            self._merge_owners(data, kind, sources, target, description)
        mapping = {identifier: target for identifier in sources if identifier != target}
        data = _document(_rewrite(data, mapping))
        self._deduplicate_refs(data)
        provenance, aliases = self._merged_provenance(sources, target)
        self._commit(data, provenance, aliases)
        return {"status": "merged", "handle": target, "revision": self.revision}

    def _merge_candidates(self, data: Document, sources: list[str], target: str) -> None:
        """只允许同一拥有者且前提一致的候选显式归并."""
        entries = _index(data)
        owners = {entries[identifier][2] for identifier in sources}
        requirements = {_dump(entries[identifier][1].get("requires", [])) for identifier in sources}
        if len(owners) != 1 or len(requirements) != 1:
            msg = "Merge owners first; candidates with different prerequisites must remain distinct"
            raise ValueError(msg)
        owner = entries[str(entries[target][2])][1]
        owner["candidates"] = [item for item in _objects(owner["candidates"]) if item["id"] not in sources or item["id"] == target]

    def _merge_owners(self, data: Document, kind: str, sources: list[str], target: str, description: str | None) -> None:
        """合并拥有者保留其全部候选, 不依据同名自动丢弃."""
        entries = _index(data)
        destination = entries[target][1]
        for identifier in sources:
            if identifier == target:
                continue
            source = entries[identifier][1]
            if kind == "feature" and set(cast("list[str]", source["element_ids"])) != set(cast("list[str]", destination["element_ids"])):
                msg = "Features covering different elements must remain distinct to preserve candidate applicability"
                raise ValueError(msg)
            if kind == "relation" and (source["kind"] != destination["kind"] or source["participants"] != destination["participants"]):
                msg = "Relations with different kinds or endpoints must remain distinct"
                raise ValueError(msg)
            for field in ("candidates", "element_ids", "feature_refs", "relation_refs", "unresolved", "composition"):
                if field in source:
                    values = cast("list[object]", destination.setdefault(field, []))
                    values.extend(value for value in cast("list[object]", source[field]) if value not in values)
        if description is not None:
            destination[{"sketch": "composition", "feature": "appearance", "relation": "description"}[kind]] = (
                [description] if kind == "sketch" else description
            )
        data[SECTIONS[kind]] = [item for item in _objects(data[SECTIONS[kind]]) if item["id"] not in sources or item["id"] == target]

    def _merged_provenance(self, sources: list[str], target: str) -> tuple[dict[str, list[Document]], dict[str, str]]:
        """来源归集与别名保存在业务四库以外."""
        provenance, aliases = deepcopy(self.provenance), dict(self.aliases)
        for identifier in sources:
            if identifier != target:
                provenance[target].extend(provenance.pop(identifier))
                aliases[identifier] = target
        return provenance, aliases

    @staticmethod
    def _deduplicate_refs(data: Document) -> None:
        """机械折叠合并后的完全相同引用, 冲突范围交由业务校验拒绝."""
        for sketch in _objects(data["sketch_library"]):
            for section in ("feature_refs", "relation_refs"):
                references: list[Document] = []
                for reference in _objects(sketch.get(section, [])):
                    choices: list[Document] = []
                    for candidate in _objects(reference.get("candidate_refs", [])):
                        if candidate not in choices:
                            choices.append(candidate)
                    reference["candidate_refs"] = choices
                    key = "feature_id" if section == "feature_refs" else "relation_id"
                    matching = next(
                        (item for item in references if item[key] == reference[key] and item.get("applies_to") == reference.get("applies_to")), None
                    )
                    if matching is None:
                        references.append(reference)
                    else:
                        existing = _objects(matching["candidate_refs"])
                        existing.extend(choice for choice in choices if choice not in existing)
                        matching["candidate_refs"] = existing
                sketch[section] = references

    def update_sketch(self, sketch: FinalSketch) -> Document:
        """更新已读草图的适用引用和局部排序, 不删除库中候选."""
        identifier = self.resolve(sketch.id)
        if identifier != sketch.id or identifier not in self._entries() or self._entries()[identifier][0] != "sketch":
            msg = "Use the current canonical sketch identifier"
            raise ValueError(msg)
        current = next(item for item in self._library.sketch_library if item.id == identifier)
        if sketch.composition != current.composition or sketch.element_ids != current.element_ids:
            msg = "Sketch composition and element scope cannot be replaced; add a new sketch to preserve the existing possibility"
            raise ValueError(msg)
        replacement = _document(json.loads(_dump(asdict(sketch))))
        dependencies = _dependencies(("sketch", replacement, None))
        self._require_presented([identifier, *dependencies])
        data = self._data()
        data["sketch_library"] = [replacement if item["id"] == identifier else item for item in _objects(data["sketch_library"])]
        provenance = deepcopy(self.provenance)
        provenance[identifier].append({"author": "main", "operation": "update_sketch", "revision": self.revision + 1})
        self._commit(data, provenance, dict(self.aliases))
        return {"status": "updated", "handle": identifier, "revision": self.revision}

    def update_elements_unresolved(self, unresolved: dict[str, list[str]]) -> None:
        """保留没有草图承载的具体未解问题, 不改变元素身份."""
        data = self._data()
        elements = {str(item["id"]): item for item in _objects(data["elements"])}
        if set(unresolved) - set(elements):
            msg = "Unknown element in unresolved problems"
            raise ValueError(msg)
        for identifier, problems in unresolved.items():
            elements[identifier]["unresolved"] = problems
        self._commit(data, deepcopy(self.provenance), dict(self.aliases))

    def add_sketch(self, sketch: FinalSketch) -> Document:
        """登记跨报告组成, 保留主 Agent 作者身份及现有候选正文."""
        if sketch.id in self._entries() or sketch.id in self.aliases:
            msg = "A new sketch must have an unused identifier"
            raise ValueError(msg)
        addition = _document(json.loads(_dump(asdict(sketch))))
        self._require_presented(_dependencies(("sketch", addition, None)))
        data, provenance = self._data(), deepcopy(self.provenance)
        cast("list[object]", data["sketch_library"]).append(addition)
        provenance[sketch.id] = [{"author": "main", "operation": "add_sketch", "revision": self.revision + 1}]
        self._commit(data, provenance, dict(self.aliases))
        return {"status": "added", "handle": sketch.id, "revision": self.revision}

    def revise_outline(self, outline: VisualOutline, element_mapping: dict[str, str], group_mapping: dict[str, str]) -> Document:
        """在明确一对一迁移下修订初稿, 歧义拆分与悬空引用均拒绝发布."""
        validate_outline(outline)
        self._validate_migration(outline, element_mapping, group_mapping)
        data = self._data()
        migrated = _document(_migrate_elements(data, element_mapping, group_mapping))
        old_elements = {str(item["id"]): item for item in _objects(migrated["elements"])}
        migrated["elements"] = [
            {
                **{key: value for key, value in asdict(element).items() if key != "salient_features"},
                "unresolved": old_elements.get(element.id, {}).get("unresolved", []),
            }
            for element in outline.elements
        ]
        before, after = _index(data), _index(migrated)
        affected = [identifier for identifier in before if before[identifier] != after[identifier]]
        old_identity = {item.id: item for item in self.outline.elements}
        if any(element.id in old_identity and old_identity[element.id] != element for element in outline.elements):
            affected = list(before)
        self._require_presented(affected)
        self._commit(migrated, deepcopy(self.provenance), dict(self.aliases), outline=outline)
        self.outline = outline
        return {"status": "outline_updated", "revision": self.revision, "affected_handles": affected}

    def _validate_migration(self, outline: VisualOutline, elements: dict[str, str], groups: dict[str, str]) -> None:
        """映射只作用于真实旧身份, 不允许多对一或覆盖仍被保留的身份."""
        old_elements = {item.id for item in self.outline.elements}
        new_elements = {item.id for item in outline.elements}
        if any(item.unresolved and elements.get(item.id, item.id) not in new_elements for item in self._library.elements):
            msg = "Elements with unresolved problems require an explicit identity migration before removal"
            raise ValueError(msg)
        old_groups = {group.id for item in self.outline.elements for group in item.instance_groups}
        new_groups = {group.id for item in outline.elements for group in item.instance_groups}
        for mapping, old_ids, new_ids in ((elements, old_elements, new_elements), (groups, old_groups, new_groups)):
            if set(mapping) - old_ids or set(mapping.values()) - new_ids:
                msg = "Mappings must refer to old identities and their explicit new destinations"
                raise ValueError(msg)
            destinations = [mapping.get(identifier, identifier) for identifier in old_ids if identifier in mapping or identifier in new_ids]
            if len(destinations) != len(set(destinations)):
                msg = "Only unambiguous one-to-one identity migrations are supported"
                raise ValueError(msg)


def _migrate_elements(value: object, elements: dict[str, str], groups: dict[str, str]) -> object:
    """仅替换元素/组引用, 自由文本和候选身份不参与机械迁移."""
    if isinstance(value, list):
        return [_migrate_elements(item, elements, groups) for item in value]
    if not isinstance(value, dict):
        return value
    result: Document = {}
    for key, child in value.items():
        if key == "element_ids":
            result[key] = [elements.get(str(identifier), str(identifier)) for identifier in child]
        elif key == "element_id" or (key == "id" and value.get("kind") == "element"):
            result[key] = elements.get(str(child), child)
        elif key == "group_id":
            result[key] = groups.get(str(child), child)
        elif key == "elements":
            result[key] = [
                {**_document(_migrate_elements(item, elements, groups)), "id": elements.get(str(item["id"]), item["id"])} for item in _objects(child)
            ]
        else:
            result[key] = _migrate_elements(child, elements, groups)
    return result
