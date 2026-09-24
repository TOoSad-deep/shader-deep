"""不可变探索报告、可能性库增量整合及实际材料呈现登记."""

from __future__ import annotations

import hashlib
import json
import re
from copy import copy, deepcopy
from dataclasses import asdict
from threading import RLock
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from pydantic import TypeAdapter, ValidationError

from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.library import reading
from shader_deep.domain.library.documents import (
    _compact,
    _dependencies,
    _document,
    _dump,
    _final_report,
    _index,
    _migrate_elements,
    _objects,
    _read_edges,
    _rewrite,
)
from shader_deep.domain.library.merging import _deduplicate_refs, _merge_candidates, _merge_owners
from shader_deep.domain.library.models import ExplorationReport, FinalSketch, PossibilityLibrary, VisualOutline
from shader_deep.domain.library.validation import validate_exploration, validate_library, validate_outline

if TYPE_CHECKING:
    from shader_deep.domain.library.decisions import IntegrationDecision
    from shader_deep.domain.library.persistence import LibraryPersistence

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


class LibraryStore:
    """管理完整四库, 仅允许显式且可追溯的语义整合."""

    def __init__(self, outline: VisualOutline, persistence: LibraryPersistence, *, max_response_chars: int = MAX_LIBRARY_RESPONSE_CHARS) -> None:
        """绑定已校验初稿和持久化接口, 外部资源由调用方提供."""
        validate_outline(outline)
        self.outline = outline
        self.persistence = persistence
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
                _deduplicate_refs({"sketch_library": [replacement]})
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
        self.persistence.save_version(revision, snapshot)
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
        """委托持久化端保留不可变原报告, 不覆盖已接受内容."""
        if self._staging:
            return
        self.persistence.save_source(report_id, asdict(report))

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
        return reading._read_object(self, identifier, entries)

    def _chunks(self, identifier: str, item: Document) -> list[str]:
        """对过大的单个对象编码做无损分段, 正文未完整呈现前不算已读."""
        return reading._chunks(self, identifier, item)

    def _materials(self, handles: list[str], entries: dict[str, tuple[str, Document, str | None]]) -> list[str]:
        """普通对象保持完整, 过大对象转为可逐页读取的有限片段序列."""
        return reading._materials(self, handles, entries)

    def _material(self, handle: str, entries: dict[str, tuple[str, Document, str | None]]) -> Document:
        """取得请求材料, 片段也是独立合法 JSON 对象."""
        return reading._material(self, handle, entries)

    def read(self, identifier: str, tool_call_id: str | None = None) -> str:
        """读取业务闭包; 过大单项以带摘要和偏移的编码片段无损传输."""
        return reading.read(self, identifier, tool_call_id)

    def _accept_fragment(self, handle: str, body: str, entries: dict[str, tuple[str, Document, str | None]]) -> None:
        """只有当前对象版本全部片段实际呈现, 才授予该对象已读身份."""
        return reading._accept_fragment(self, handle, body, entries)

    def present_tool_results(self, results: list[tuple[str, object, bool]]) -> int:
        """仅登记原调用 ID 下实际进入请求且未被截断的工具正文."""
        return reading.present_tool_results(self, results)

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
            _merge_candidates(data, sources, target)
        else:
            _merge_owners(data, kind, sources, target, description)
        mapping = {identifier: target for identifier in sources if identifier != target}
        data = _document(_rewrite(data, mapping))
        _deduplicate_refs(data)
        provenance, aliases = self._merged_provenance(sources, target)
        self._commit(data, provenance, aliases)
        return {"status": "merged", "handle": target, "revision": self.revision}

    def _merged_provenance(self, sources: list[str], target: str) -> tuple[dict[str, list[Document]], dict[str, str]]:
        """来源归集与别名保存在业务四库以外."""
        provenance, aliases = deepcopy(self.provenance), dict(self.aliases)
        for identifier in sources:
            if identifier != target:
                provenance[target].extend(provenance.pop(identifier))
                aliases[identifier] = target
        return provenance, aliases

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
