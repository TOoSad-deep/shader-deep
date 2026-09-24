"""旧材料包调度策略, 保留现有回归与显式调用能力."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shader_deep.domain.library.materials import _dump, _group, _hash, _judgment_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shader_deep.domain.library.materials import Document, MaterialStore


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
