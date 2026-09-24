"""按完整判断材料组织整合队列, 保存版本和未完成范围."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from shader_deep.analysis.library_views import ListComparisonWorkInput, _bounded_page, _encode_cursor, _page_chars, _page_offset
from shader_deep.analysis.schemas import AnalysisValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shader_deep.analysis.integration import IntegrationDecision

Document = dict[str, object]


def _dump(value: object) -> str:
    """用实际传输形状估算剩余材料预算."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash(value: object) -> str:
    """绑定材料外的初稿及问题记录."""
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _work_index(work: Document) -> Document:
    """工作目录不携带已选正文、旧决定或呈现记录."""
    keys = ("work_id", "question", "target_ids", "status", "reason", "source_work_id")
    return {**{key: deepcopy(work[key]) for key in keys if key in work}, "target_count": len(cast("list[str]", work["target_ids"]))}


def _compact_work_index(row: Document) -> Document:
    """超大工作仍保留身份与目标查询入口, 不用摘要替代判断依据."""
    keys = ("work_id", "status", "target_count", "source_work_id")
    return {
        **{key: row[key] for key in keys if key in row},
        "details_omitted": True,
        "details_query": {"work_id": row["work_id"]},
        "target_query": {"work_id": row["work_id"]},
    }


def _work_detail(row: Document, query: ListComparisonWorkInput, fingerprint: str, max_chars: int) -> Document:
    """指定工作仍受预算约束; 问题完整提供或明确不可呈现, 目标可继续分页."""
    targets = cast("list[str]", row["target_ids"])
    target_fingerprint = _hash({"ledger": fingerprint, "work_id": row["work_id"]})
    offset = _page_offset(query.target_cursor, target_fingerprint, len(targets), allow_start=True)
    result = {
        **row,
        "target_ids": [],
        "target_remaining": len(targets) - offset,
        "target_next_cursor": _encode_cursor(target_fingerprint, offset) if offset < len(targets) else None,
    }
    if _page_chars({**result, "target_ids": targets[offset : offset + 1]}) > max_chars:
        result = {
            **_compact_work_index(row),
            "target_ids": [],
            "target_remaining": len(targets) - offset,
            "target_next_cursor": result["target_next_cursor"],
            "details_unavailable_due_to_budget": True,
        }
    selected: list[str] = []
    for identifier in targets[offset : offset + query.limit]:
        end = offset + len(selected) + 1
        candidate = {
            **result,
            "target_ids": [*selected, identifier],
            "target_remaining": len(targets) - end,
            "target_next_cursor": _encode_cursor(target_fingerprint, end) if end < len(targets) else None,
        }
        if _page_chars(candidate) > max_chars:
            break
        selected.append(identifier)
        result = candidate
    if (offset < len(targets) and not selected) or _page_chars(result) > max_chars:
        msg = "Comparison work identity exceeds page budget; increase max_chars"
        raise ValueError(msg)
    return result


class MaterialStore(Protocol):
    """材料组织只依赖库的只读投影和身份解析."""

    @property
    def revision(self) -> int:
        """返回库版本."""
        ...

    def material_entries(self) -> dict[str, Document]:
        """返回完整条目及独立候选正文."""
        ...

    def material_closure(self, identifiers: list[str]) -> list[str]:
        """返回判断所需的完整依赖闭包."""
        ...

    def material_versions(self, identifiers: list[str]) -> dict[str, str]:
        """返回各条目内容版本."""
        ...

    def resolve(self, identifier: str) -> str:
        """解析机械合并产生的别名."""
        ...


@dataclass(frozen=True)
class MaterialPackage:
    """完整业务材料与执行器持有的提交版本绑定."""

    package_id: str
    version: int
    library_revision: int
    writable_ids: tuple[str, ...]
    read_only_ids: tuple[str, ...]
    entries: tuple[Document, ...]
    content_versions: dict[str, str]
    context: Document
    context_version: str

    @property
    def id(self) -> str:
        """返回执行器绑定的包身份."""
        return self.package_id

    @property
    def versions(self) -> dict[str, str]:
        """返回判断材料内容版本的独立副本."""
        return dict(self.content_versions)

    def business_payload(self) -> Document:
        """返回模型需要的材料, 不要求模型复制后台版本台账."""
        return deepcopy(
            {
                "writable_ids": list(self.writable_ids),
                "read_only_ids": list(self.read_only_ids),
                "entries": list(self.entries),
                "context": self.context,
                "task": "比较本包可写条目, 保留不同机制; 只读关联仅作依据. 未能完成的跨包比较明确记录, 不宣称全库已去重.",
            }
        )

    @property
    def material_chars(self) -> int:
        """计入 JSON 正文嵌入消息 text 字符串后的转义开销.

        完整请求统计会再次编码文本块, 引号和反斜杠必须计两层.
        此处只扣除最外层字符串引号; 固定消息结构由请求预算另行预留.
        """
        return len(json.dumps(_dump(self.business_payload()), ensure_ascii=False)) - 2


def _group(entry: Document) -> tuple[str, str]:
    """优先共同呈现同类型及相同元素范围的条目."""
    content = cast("Document", entry.get("content", {}))
    scope = content.get("element_ids", content.get("participants", []))
    return str(entry.get("kind", "")), _dump(scope)


def _canonical(value: object, store: MaterialStore, *, key: str = "") -> object:
    """规范化纯身份改写和派生计数, 不改机制及理由原文."""
    if isinstance(value, dict):
        return {name: _canonical(child, store, key=name) for name, child in value.items() if name != "candidate_count"}
    if isinstance(value, list):
        values = [_canonical(child, store, key=key) for child in value]
        if key in {"candidate_ids", "candidate_refs"}:
            return _unique(values)
        if key in {"feature_refs", "relation_refs"}:
            return _canonical_choices(values)
        return values
    identity_keys = {"id", "handle", "owner", "feature_id", "relation_id", "candidate_id", "candidate_ids"}
    return store.resolve(value) if key in identity_keys and isinstance(value, str) else value


def _unique(values: list[object]) -> list[object]:
    """引用集合忽略机械重排及完全重复项."""
    indexed = {json.dumps(value, sort_keys=True, ensure_ascii=False): value for value in values}
    return [indexed[key] for key in sorted(indexed)]


def _canonical_choices(values: list[object]) -> list[object]:
    """引用合并拆为相同粒度的选择集合, 不丢失范围和优先级."""
    choices: list[object] = []
    for value in values:
        reference = cast("Document", value)
        base = {key: child for key, child in reference.items() if key != "candidate_refs"}
        candidates = cast("list[object]", reference.get("candidate_refs", []))
        choices.extend({**base, "candidate_ref": candidate} for candidate in candidates)
        if not candidates:
            choices.append(base)
    return _unique(choices)


def _judgment_fingerprint(entries: tuple[Document, ...], store: MaterialStore) -> str:
    """比较完整判断前提, 别名变化本身不构成新的判断任务."""
    canonical = _unique([_canonical(entry, store) for entry in entries])
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MaterialQueue:
    """维护完整单元队列, 超预算范围只暂缓一次.

    Args:
        material_budget_chars: 扣除其他请求部分和输出预留后的材料字符预算.
        context: 同包呈现并绑定版本的统一初稿, 问题和失败记录.
    """

    def __init__(self, material_budget_chars: int, *, context: Document | None = None) -> None:
        """初始化后端队列, 不读取或声明模型已看到材料."""
        if material_budget_chars < 1:
            msg = "material_budget_chars must be positive"
            raise ValueError(msg)
        self.material_budget_chars = material_budget_chars
        self.context = deepcopy(context or {})
        self._pending: list[str] = []
        self._known: set[str] = set()
        self._completed: set[str] = set()
        self._reviewed: dict[str, tuple[Document, ...]] = {}
        self._reviews: list[Document] = []
        self._deferred: list[Document] = []
        self._active: MaterialPackage | None = None
        self._sequence = 0
        self._version = 1

    def update_context(self, context: Document) -> None:
        """更新问题材料, 下一次取包时刷新过期包而非沿用旧判断."""
        self.context = deepcopy(context)

    def _synchronize(self, store: MaterialStore) -> dict[str, Document]:
        """新增外部内容加入队列; 已处理条目的机械变化不重新排队."""
        entries = store.material_entries()
        roots = [identifier for identifier, entry in entries.items() if entry.get("kind") not in {"candidate", "element"}]
        self._pending.extend(identifier for identifier in roots if identifier not in self._known)
        self._known.update(entries)
        self._pending = list(dict.fromkeys(store.resolve(identifier) for identifier in self._pending if store.resolve(identifier) in entries))
        return entries

    def _build(self, store: MaterialStore, roots: list[str], entries: dict[str, Document]) -> MaterialPackage:
        """一个拥有者及全部候选构成可写单元, 依赖保持只读."""
        writable = set(roots)
        writable.update(identifier for identifier, entry in entries.items() if entry.get("owner") in roots)
        closure = store.material_closure(list(dict.fromkeys([*roots, *sorted(writable)])))
        return MaterialPackage(
            package_id=f"integration-{self._sequence + 1}",
            version=self._version,
            library_revision=store.revision,
            writable_ids=tuple(sorted(writable)),
            read_only_ids=tuple(identifier for identifier in closure if identifier not in writable),
            entries=tuple(deepcopy(entries[identifier]) for identifier in closure),
            content_versions=store.material_versions(closure),
            context=deepcopy(self.context),
            context_version=_hash(self.context),
        )

    def is_current(self, store: MaterialStore, package: MaterialPackage) -> bool:
        """按实际判断内容验证版本, 不因无关库版本递增拒绝提交."""
        try:
            versions = store.material_versions(list(package.content_versions))
        except (KeyError, ValueError):
            return False
        return versions == package.content_versions and _hash(self.context) == package.context_version

    def next_package(self, store: MaterialStore) -> MaterialPackage | None:
        """优先整包, 否则按完整单元贪心分包; 超大单元记录后跳过."""
        entries = self._synchronize(store)
        if self._active is not None:
            if self.is_current(store, self._active):
                return self._active
            self._version += 1
            self._active = None
        if not self._pending:
            return None
        complete = self._build(store, self._pending, entries)
        if complete.material_chars <= self.material_budget_chars:
            self._active = complete
        else:
            self._active = self._split(store, entries)
        return self._active

    def _split(self, store: MaterialStore, entries: dict[str, Document]) -> MaterialPackage | None:
        """优先装入同类完整单元, 不切断正文或依赖."""
        chosen: list[str] = []
        for identifier in sorted(self._pending, key=lambda item: _group(entries[item])):
            unit = self._build(store, [identifier], entries)
            if unit.material_chars > self.material_budget_chars:
                self._record_deferred([identifier], "input_budget_exceeded")
                continue
            combined = self._build(store, [*chosen, identifier], entries)
            if combined.material_chars <= self.material_budget_chars:
                chosen.append(identifier)
        return self._build(store, chosen, entries) if chosen else None

    def _record_deferred(self, roots: list[str], reason: str) -> None:
        """保留未完成判断范围并移出本轮队列, 不重复尝试."""
        self._deferred.append({"ids": roots, "reason": reason, "judgment": "整合完整条目及其必要关联材料"})
        blocked = set(roots)
        self._pending = [identifier for identifier in self._pending if identifier not in blocked]

    def accept(self, store: MaterialStore, package_id: str, *, review_ids: Iterable[str] = ()) -> None:
        """提交成功后推进; 调用方须在发布前校验版本与呈现边界.

        Args:
            store: 已经原子发布批量决定的库.
            package_id: 本次已成功发布决定的包身份.
            review_ids: 实质前提变化或新增比较缺口需要再次处理的范围.
        """
        package = self._require_active(package_id)
        completed = {store.resolve(identifier) for identifier in package.writable_ids}
        self._completed = {store.resolve(identifier) for identifier in self._completed}
        self._completed.update(completed)
        self._refresh_judgments(store, completed)
        self._pending = [identifier for identifier in self._pending if store.resolve(identifier) not in completed]
        entries = store.material_entries()
        new_roots = {identifier for identifier, entry in entries.items() if identifier not in self._known and entry.get("kind") != "candidate"}
        self._remember_judgments(store, completed | new_roots, entries)
        self._known.update(entries)
        self._advance()
        self.enqueue_review(store, review_ids)

    def _judgment(self, store: MaterialStore, root: str, entries: dict[str, Document]) -> tuple[Document, ...]:
        """保存一个完整判断单元的正文快照, 不把闭包节点当成已处理根."""
        return tuple(deepcopy(entries[identifier]) for identifier in store.material_closure([root]))

    def _refresh_judgments(self, store: MaterialStore, current: set[str]) -> None:
        """只复核先前完成范围的实质变化, 去掉旧基线避免重复排队."""
        entries = store.material_entries()
        for old_root, before in list(self._reviewed.items()):
            root = store.resolve(old_root)
            if root in current or root not in entries:
                del self._reviewed[old_root]
                continue
            after = self._judgment(store, root, entries)
            if _judgment_fingerprint(before, store) != _judgment_fingerprint(after, store):
                self.enqueue_review(store, [root])
                self._reviews.append({"ids": [root], "reason": "substantive_material_change"})
                self._completed.discard(root)
                del self._reviewed[old_root]

    def _remember_judgments(self, store: MaterialStore, roots: set[str], entries: dict[str, Document]) -> None:
        """只有实际完成的可写根获得判断基线; 只读闭包不升级为完成."""
        for root in roots:
            if root in entries and entries[root].get("kind") != "candidate":
                self._reviewed[root] = self._judgment(store, root, entries)

    def enqueue_review(self, store: MaterialStore, identifiers: Iterable[str]) -> None:
        """显式排入实质复核范围, 将候选归到完整拥有者单元."""
        entries = store.material_entries()
        for requested in identifiers:
            identifier = store.resolve(requested)
            if identifier not in entries:
                msg = f"Unknown review handle: {requested}"
                raise ValueError(msg)
            owner = entries[identifier].get("owner")
            root = str(owner) if owner is not None else identifier
            if root not in self._pending:
                self._pending.append(root)

    def defer(self, reason: str = "input_budget_exceeded") -> None:
        """完整请求预算或修复耗尽时保存未完成范围并推进."""
        if self._active is None:
            msg = "No active material package"
            raise ValueError(msg)
        roots = [identifier for identifier in self._pending if identifier in self._active.writable_ids]
        self._record_deferred(roots, reason)
        self._advance()

    def _require_active(self, package_id: str) -> MaterialPackage:
        """拒绝将其他包的回执误用于当前处理进度."""
        if self._active is None or self._active.package_id != package_id:
            msg = "Unknown active material package"
            raise ValueError(msg)
        return self._active

    def _advance(self) -> None:
        """结束当前包并分配下一身份."""
        self._active = None
        self._sequence += 1
        self._version = 1

    @property
    def deferred(self) -> tuple[Document, ...]:
        """返回未完成范围的独立快照."""
        return tuple(deepcopy(self._deferred))

    def snapshot(self) -> Document:
        """返回可持久化的后台进度, 不宣称恢复模型历史."""
        active: Document | None = None
        if self._active is not None:
            active = {
                "package_id": self._active.package_id,
                "version": self._active.version,
                "library_revision": self._active.library_revision,
                "content_versions": self._active.content_versions,
                "context_version": self._active.context_version,
                "writable_ids": list(self._active.writable_ids),
                "read_only_ids": list(self._active.read_only_ids),
            }
        return deepcopy(
            {"pending": self._pending, "completed": sorted(self._completed), "deferred": self._deferred, "reviews": self._reviews, "active": active}
        )


@dataclass
class ComparisonGroup:
    """显式比较范围、选取正文和实际呈现版本分别保存."""

    id: str
    question: str
    target_ids: tuple[str, ...]
    version: int = 1
    library_revision: int = 0
    entries: dict[str, Document] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    presented_versions: dict[str, str] = field(default_factory=dict)
    gaps: list[Document] = field(default_factory=list)
    work_context: Document = field(default_factory=dict)

    def business_payload(self) -> Document:
        """只提供当前组正文, 不递归加载引用或其他工作的材料."""
        return deepcopy(
            {
                "work_id": self.id,
                "version": self.version,
                "question": self.question,
                "target_ids": list(self.target_ids),
                "entries": list(self.entries.values()),
                "work_context": self.work_context,
            }
        )


def _comparison_error(path: str, message: str) -> None:
    """错误绑定真实决定字段, 供现有草稿修复入口使用."""
    raise AnalysisValidationError([{"path": path, "code": "invalid_comparison", "message": message}])


class ComparisonManager:
    """保存按需比较工作, 不把读取或辅助材料自动算为已比较."""

    def __init__(self) -> None:
        """初始化空工作记录; 未展开条目不会自动产生必要任务."""
        self.active: ComparisonGroup | None = None
        self._works: dict[str, Document] = {}
        self._baselines: dict[str, tuple[Document, ...]] = {}
        self._sequence = 0

    @staticmethod
    def _targets(store: MaterialStore, identifiers: Iterable[str]) -> tuple[str, ...]:
        """解析别名但保持候选粒度, 不提升到整个拥有者."""
        entries = store.material_entries()
        targets = tuple(dict.fromkeys(store.resolve(identifier) for identifier in identifiers))
        missing = set(targets) - entries.keys()
        if missing:
            msg = "Unknown comparison targets: " + ", ".join(sorted(missing))
            raise ValueError(msg)
        return targets

    def _new_work(self, question: str, targets: tuple[str, ...], *, reason: str = "", source: str = "") -> str:
        """每个比较问题拥有独立身份, 不按条目覆盖其他未完成范围."""
        self._sequence += 1
        identifier = f"comparison-{self._sequence}"
        self._works[identifier] = {
            "work_id": identifier,
            "question": question,
            "target_ids": list(targets),
            "status": "pending",
            "reason": reason,
            "source_work_id": source,
        }
        return identifier

    def start(self, store: MaterialStore, question: str, target_ids: list[str], *, work_id: str | None = None) -> ComparisonGroup:
        """新建比较或接续指定必要工作, 拒绝静默替换已有组.

        Args:
            store: 当前材料库.
            question: 本组需要回答的问题; 接续工作时使用已存问题.
            target_ids: 显式目标; 接续工作时使用已存范围.
            work_id: 待处理或暂缓工作身份.

        Returns:
            尚无已选正文的当前组.
        """
        if self.active is not None:
            msg = "An active comparison must be accepted or deferred before starting another"
            raise ValueError(msg)
        if work_id is None:
            targets = self._targets(store, target_ids)
            if not question.strip() or not targets:
                msg = "A comparison requires a question and at least one target"
                raise ValueError(msg)
            work_id = self._new_work(question, targets)
        else:
            work = self._works.get(work_id)
            if work is None or work["status"] not in {"pending", "deferred"}:
                msg = "Unknown or already completed comparison work"
                raise ValueError(msg)
            targets = self._targets(store, cast("list[str]", work["target_ids"]))
            question = str(work["question"])
        version = cast("int", self._works[work_id].get("version", 0)) + 1
        self._works[work_id].update(status="comparing", target_ids=list(targets), version=version)
        work = self._works[work_id]
        # 接续理由属于本组判断依据, 不能只依赖可能被分页省略的工作索引.
        context = {key: deepcopy(work[key]) for key in ("reason", "gaps") if work.get(key)}
        self.active = ComparisonGroup(work_id, question, targets, version=version, library_revision=store.revision, work_context=context)
        return self.active

    def _require_active(self) -> ComparisonGroup:
        """没有当前组时拒绝修改后台比较进度."""
        if self.active is None:
            msg = "No active comparison"
            raise ValueError(msg)
        return self.active

    def extend_targets(self, store: MaterialStore, identifiers: Iterable[str]) -> ComparisonGroup:
        """只有显式扩展才将辅助材料升级为本组修改和完成范围."""
        group = self._require_active()
        targets = tuple(dict.fromkeys((*group.target_ids, *self._targets(store, identifiers))))
        if targets != group.target_ids:
            group.target_ids = targets
            group.version += 1
            self._works[group.id].update(target_ids=list(targets), version=group.version)
        return group

    def append(self, entries: dict[str, Document], versions: dict[str, str], *, library_revision: int) -> ComparisonGroup:
        """追加预算准入后的完整条目; 选取不登记实际呈现."""
        group = self._require_active()
        if entries.keys() != versions.keys():
            msg = "Selected entries and versions must have identical identifiers"
            raise ValueError(msg)
        changed = {identifier for identifier, version in versions.items() if group.versions.get(identifier) != version}
        if changed:
            group.entries.update(deepcopy(entries))
            group.versions.update(versions)
            for identifier in changed:
                group.presented_versions.pop(identifier, None)
            group.version += 1
            self._works[group.id]["version"] = group.version
        group.library_revision = library_revision
        return group

    def mark_presented(self, versions: dict[str, str]) -> None:
        """只登记 Context Builder 实际准备发送的当前版本."""
        group = self._require_active()
        if any(group.versions.get(identifier) != version for identifier, version in versions.items()):
            msg = "Presented materials must match current selected versions"
            raise ValueError(msg)
        group.presented_versions.update(versions)

    def is_current(self, store: MaterialStore, group: ComparisonGroup) -> bool:
        """无关条目更新不使当前组选材失效."""
        try:
            return store.material_versions(list(group.versions)) == group.versions
        except ValueError:
            return False

    def refresh(self, store: MaterialStore) -> ComparisonGroup | None:
        """保留问题和工作身份, 只刷新过期正文并撤销其呈现登记."""
        self._reopen_changed(store)
        group = self.active
        if group is None or self.is_current(store, group):
            return group
        entries = store.material_entries()
        selected = tuple(dict.fromkeys(store.resolve(identifier) for identifier in group.entries))
        available = [identifier for identifier in selected if identifier in entries]
        versions = store.material_versions(available)
        group.entries = {identifier: entries[identifier] for identifier in available}
        group.presented_versions = {
            identifier: version for identifier, version in group.presented_versions.items() if versions.get(identifier) == version
        }
        group.versions = versions
        group.target_ids = tuple(dict.fromkeys(store.resolve(identifier) for identifier in group.target_ids))
        group.version += 1
        group.library_revision = store.revision
        self._works[group.id].update(target_ids=list(group.target_ids), version=group.version)
        return group

    @staticmethod
    def _changed_ids(decision: IntegrationDecision) -> set[str]:
        """机械受影响引用不扩大本次显式语义修改范围."""
        return (
            {member for merge in decision.merges for member in merge.members}
            | {sketch.id for sketch in decision.reference_updates}
            | set(decision.unresolved)
        )

    def validate_decision(self, store: MaterialStore, decision: IntegrationDecision) -> None:
        """发布前检查目标处置完整性; 业务材料前置条件仍由库检查."""
        group = self._require_active()
        if decision.preserve and decision.deferred_work:
            _comparison_error("/preserve", "preserve and deferred_work are mutually exclusive")
        if not self.is_current(store, group):
            _comparison_error("/", "Comparison materials changed; refresh before submitting")
        self._validate_scope(group, decision)
        targets = set(group.target_ids)
        missing = targets - group.presented_versions.keys()
        if missing and not decision.deferred_work:
            _comparison_error("/preserve", "Read and present comparison target bodies: " + ", ".join(sorted(missing)))
        untouched = targets - self._changed_ids(decision)
        if untouched and not decision.preserve and not decision.deferred_work:
            _comparison_error("/preserve", "Unaddressed comparison targets: " + ", ".join(sorted(untouched)))
        self._validate_reviews(store, decision)

    def _validate_reviews(self, store: MaterialStore, decision: IntegrationDecision) -> None:
        """新增局部身份由库在同批发布时解析, 既有身份仍提前校验."""
        added: set[str] = set()
        if decision.additions is not None:
            report = decision.additions
            owners = (*report.sketch_library, *report.feature_library, *report.relation_library)
            added.update(owner.id for owner in owners)
            added.update(candidate.id for owner in (*report.feature_library, *report.relation_library) for candidate in owner.candidates)
        existing = store.material_entries()
        for index, identifier in enumerate(decision.review_ids):
            if identifier not in added and store.resolve(identifier) not in existing:
                _comparison_error(f"/review_ids/{index}", f"Unknown review identifier: {identifier}")

    @staticmethod
    def _validate_scope(group: ComparisonGroup, decision: IntegrationDecision) -> None:
        """现有语义操作只能针对显式目标, 辅助读取不授予修改权."""
        targets = set(group.target_ids)
        for index, merge in enumerate(decision.merges):
            for position, member in enumerate(merge.members):
                if member not in targets:
                    _comparison_error(f"/merges/{index}/members/{position}", "Merge member is outside comparison targets")
        for index, sketch in enumerate(decision.reference_updates):
            if sketch.id not in targets:
                _comparison_error(f"/reference_updates/{index}/id", "Sketch is outside comparison targets")
        if set(decision.unresolved) - targets:
            _comparison_error("/unresolved", "Existing element changes must be explicit comparison targets")

    def accept(self, store: MaterialStore, decision: IntegrationDecision, receipt: Document) -> Document:
        """库原子发布成功后关闭绑定工作, 其他必要工作保持独立."""
        group = self._require_active()
        changed = self._changed_ids(decision)
        status = "deferred" if decision.deferred_work else "modified" if changed or decision.additions else "compared_preserved"
        work = self._works[group.id]
        work.update(status=status, decision=decision.model_dump(mode="json"), receipt=deepcopy(receipt), reason="; ".join(decision.deferred_work))
        work["modified_ids"] = sorted(changed)
        work["preserved_ids"] = sorted(set(group.target_ids) - changed) if decision.preserve else []
        work["selected_ids"] = list(group.entries)
        work["presented_versions"] = dict(group.presented_versions)
        self._remember(store, group)
        self.active = None
        self._reopen_changed(store, exclude=group.id)
        if decision.review_ids:
            targets = self._targets(store, cast("list[str]", receipt.get("review_ids", list(decision.review_ids))))
            self._new_work("复核明确指定的条目范围", targets, reason="explicit_review", source=group.id)
        return deepcopy(work)

    def _remember(self, store: MaterialStore, group: ComparisonGroup) -> None:
        """保存接受后的判断前提, 辅助材料变化也可触发原问题复核."""
        entries = store.material_entries()
        identifiers = tuple(dict.fromkeys(store.resolve(identifier) for identifier in group.presented_versions))
        self._baselines[group.id] = tuple(deepcopy(entries[identifier]) for identifier in identifiers if identifier in entries)

    def _reopen_changed(self, store: MaterialStore, *, exclude: str = "") -> None:
        """实质前提变化创建一次必要复核; 纯别名改写不重复分析."""
        entries = store.material_entries()
        for work_id, before in tuple(self._baselines.items()):
            if work_id == exclude or self._works[work_id]["status"] not in {"modified", "compared_preserved"}:
                continue
            identifiers = tuple(dict.fromkeys(store.resolve(str(entry["handle"])) for entry in before))
            after = tuple(entries[identifier] for identifier in identifiers if identifier in entries)
            if _judgment_fingerprint(before, store) != _judgment_fingerprint(after, store):
                work = self._works[work_id]
                targets = tuple(dict.fromkeys(store.resolve(identifier) for identifier in cast("list[str]", work["target_ids"])))
                self._new_work(str(work["question"]), targets, reason="substantive_material_change", source=work_id)
                del self._baselines[work_id]

    def defer(self, reason: str = "input_budget_exceeded") -> None:
        """保存当前组及明确缺口, 不把暂缓范围标成已比较."""
        group = self._require_active()
        self._works[group.id].update(
            status="deferred",
            reason=reason,
            selection=deepcopy(group.business_payload()),
            presented_versions=dict(group.presented_versions),
            gaps=deepcopy(group.gaps),
            content_versions=dict(group.versions),
        )
        self.active = None

    @property
    def pending(self) -> tuple[Document, ...]:
        """所有未关闭必要工作, 包括暂缓和当前比较."""
        return tuple(deepcopy(work) for work in self._works.values() if work["status"] in {"pending", "comparing", "deferred"})

    @property
    def deferred(self) -> tuple[Document, ...]:
        """返回缺口形状兼容的暂缓范围副本."""
        return tuple(
            {**deepcopy(work), "ids": list(cast("list[str]", work["target_ids"]))} for work in self._works.values() if work["status"] == "deferred"
        )

    def status_summary(self, store: MaterialStore) -> dict[str, str]:
        """未完成范围优先显示, 不被同一条目的其他已完成比较遮盖."""
        self._reopen_changed(store)
        result = dict.fromkeys(store.material_entries(), "unexpanded_preserved")
        priority = {"unexpanded_preserved": 0, "compared_preserved": 1, "modified": 2, "deferred": 3, "pending": 4, "comparing": 5}
        for work in self._works.values():
            status = str(work["status"])
            for requested in cast("list[str]", work["target_ids"]):
                identifier = store.resolve(requested)
                target_status = (
                    "compared_preserved" if status == "modified" and requested in cast("list[str]", work.get("preserved_ids", [])) else status
                )
                if identifier in result and priority[target_status] > priority[result[identifier]]:
                    result[identifier] = target_status
        return result

    def snapshot(self) -> Document:
        """持久化范围、呈现依据和缺口; 不声明恢复模型历史."""
        group = self.active
        active = (
            None
            if group is None
            else {
                **group.business_payload(),
                "version": group.version,
                "library_revision": group.library_revision,
                "content_versions": group.versions,
                "presented_versions": group.presented_versions,
                "gaps": group.gaps,
            }
        )
        return deepcopy(
            {
                "works": list(self._works.values()),
                "pending": list(self.pending),
                "deferred": list(self.deferred),
                "active": active,
                "judgment_baselines": self._baselines,
            }
        )

    def list_work(
        self,
        *,
        statuses: list[str] | None = None,
        work_id: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
        target_cursor: str | None = None,
        max_chars: int = 8000,
    ) -> Document:
        """只读分页查询完整台账的轻量索引, 工作变化后旧游标失效.

        Args:
            statuses: 可选工作状态过滤.
            work_id: 查询单个工作的完整问题与原因, 超限则明确报告.
            cursor: 上页工作游标.
            limit: 工作页或指定工作目标页的最大条目数.
            target_cursor: 指定工作内的下一页目标游标.
            max_chars: 完整工具回执的字符上限.

        Returns:
            工作索引、全局台账版本、总数、剩余数及后续游标.

        Raises:
            ValueError: 查询、游标非法或最小身份也无法容纳.
        """
        query = ListComparisonWorkInput(statuses=statuses or [], work_id=work_id, cursor=cursor, limit=limit, target_cursor=target_cursor)
        rows, fingerprint = self._work_query_rows(query)
        offset = _page_offset(cursor, fingerprint, len(rows))
        if work_id and rows:
            rows = [_work_detail(rows[0], query, fingerprint, max_chars - 256)]
        elif target_cursor:
            msg = "Unknown comparison work for target_cursor"
            raise ValueError(msg)
        page = _bounded_page(
            rows,
            fingerprint=fingerprint,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
            compact=_compact_work_index,
            metadata={"revision": fingerprint},
        )
        page["works"] = page.pop("entries")
        return page

    def _work_query_rows(self, query: ListComparisonWorkInput) -> tuple[list[Document], str]:
        """默认首页与显式分页共用排序和游标绑定, 不形成两套发现范围."""
        fingerprint = _hash({"ledger": self._works, "query": query.model_dump(exclude={"cursor", "target_cursor", "limit"})})
        ordered = sorted(self._works.values(), key=lambda work: work["status"] not in {"pending", "comparing", "deferred"})
        rows = [
            _work_index(work)
            for work in ordered
            if (not query.statuses or work["status"] in query.statuses) and (not query.work_id or work["work_id"] == query.work_id)
        ]
        return rows, fingerprint

    def summary(self, *, max_records: int = 30, max_chars: int = 8000) -> Document:
        """默认只展开有界首页, 完成条件仍检查后端完整必要工作."""
        counts: dict[str, int] = {}
        for work in self._works.values():
            status = str(work["status"])
            counts[status] = counts.get(status, 0) + 1
        metadata: Document = {
            "total_work_count": len(self._works),
            "pending_work_count": sum(counts.get(status, 0) for status in ("pending", "comparing", "deferred")),
            "omitted_work_count": len(self._works),
            "status_counts": counts,
            "query_tool": "list_comparison_work",
            "active_work_id": self.active.id if self.active else None,
        }
        rows, fingerprint = self._work_query_rows(ListComparisonWorkInput())
        result = _bounded_page(
            rows if max_records > 0 else [],
            fingerprint=fingerprint,
            offset=0,
            limit=max(0, min(100, max_records)),
            max_chars=max_chars,
            compact=_compact_work_index,
            metadata=metadata,
        )
        result["works"] = result.pop("entries")
        result["omitted_work_count"] = len(self._works) - len(cast("list[Document]", result["works"]))
        result.pop("total")
        result.pop("remaining")
        if _page_chars(result) > max_chars:
            msg = "Comparison summary exceeds page budget; increase max_chars"
            raise ValueError(msg)
        return result

    def planned_progress(self, decision: IntegrationDecision) -> Document:
        """提供随库原子发布的范围和判断依据, 不提前改变运行进度."""
        group = self._require_active()
        return deepcopy(
            {
                "work_id": group.id,
                "question": group.question,
                "target_ids": list(group.target_ids),
                "selected_ids": list(group.entries),
                "presented_versions": group.presented_versions,
                "decision": decision.model_dump(mode="json"),
                "required_review_ids": list(decision.review_ids),
                "status": "deferred"
                if decision.deferred_work
                else "modified"
                if self._changed_ids(decision) or decision.additions
                else "compared_preserved",
            }
        )
