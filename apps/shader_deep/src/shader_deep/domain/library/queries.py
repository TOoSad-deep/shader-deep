"""四库的轻量目录和显式正文选择, 不展开引用依赖或登记材料呈现."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shader_deep.domain.primitives import Text  # noqa: TC001  # Pydantic 在运行时解析工具字段.

if TYPE_CHECKING:
    from collections.abc import Callable

    from shader_deep.domain.library.store import Document, LibraryStore

LibraryName = Literal["elements", "sketch_library", "feature_library", "relation_library"]
LIBRARY_KINDS = {"elements": "element", "sketch_library": "sketch", "feature_library": "feature", "relation_library": "relation"}


class ComparisonSpec(BaseModel):
    """声明本组比较问题与目标, 辅助读取不扩大此范围."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    question: Text
    target_ids: Annotated[list[Text], Field(min_length=1)]


class ReadRequest(BaseModel):
    """按所属库读取拥有者; 候选正文必须显式选择."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    library: LibraryName
    ids: Annotated[list[Text], Field(min_length=1)]
    include_candidates: bool = False
    candidate_ids: list[Text] | None = None

    @model_validator(mode="after")
    def validate_candidates(self) -> Self:
        """候选选择不能绕过拥有者或隐含打开正文展开."""
        if self.candidate_ids is not None and not self.include_candidates:
            msg = "candidate_ids requires include_candidates=true"
            raise ValueError(msg)
        if self.include_candidates and self.library not in {"feature_library", "relation_library"}:
            msg = "Only feature_library and relation_library own candidates"
            raise ValueError(msg)
        return self


class ReadLibraryInput(BaseModel):
    """首次读取建组, 接续必要工作或追加同组辅助材料."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    requests: Annotated[list[ReadRequest], Field(min_length=1)]
    comparison: ComparisonSpec | None = None
    work_id: Text | None = None
    extend_target_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_work(self) -> Self:
        """已有工作与新比较不能同时声明."""
        if self.comparison is not None and self.work_id is not None:
            msg = "comparison and work_id are mutually exclusive"
            raise ValueError(msg)
        return self


class ListLibraryInput(BaseModel):
    """按库、元素、状态或拥有者分页发现身份, 不返回机制正文."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    library: LibraryName | None = None
    element_ids: list[Text] = Field(default_factory=list)
    endpoint_ids: list[Text] = Field(default_factory=list)
    statuses: list[Text] = Field(default_factory=list)
    owner_ids: list[Text] = Field(default_factory=list)
    cursor: Text | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 30


class ListComparisonWorkInput(BaseModel):
    """分页发现必要工作; 指定工作后可继续分页查看完整目标身份."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    statuses: list[Text] = Field(default_factory=list)
    work_id: Text | None = None
    cursor: Text | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 30
    target_cursor: Text | None = None

    @model_validator(mode="after")
    def validate_target_cursor(self) -> Self:
        """目标游标始终绑定一个明确工作, 不与工作页游标混用."""
        if self.target_cursor is not None and (self.work_id is None or self.cursor is not None):
            msg = "target_cursor requires work_id and cannot be combined with cursor"
            raise ValueError(msg)
        return self


def _selected_candidates(request: ReadRequest, owners: dict[str, Document]) -> list[str]:
    """只在明确选中的拥有者内解析候选, 错误不留下部分结果."""
    available = [str(identifier) for owner in owners.values() for identifier in cast("list[str]", owner.get("candidate_ids", []))]
    selected = available if request.candidate_ids is None else request.candidate_ids
    missing = set(selected) - set(available)
    if missing:
        msg = f"Candidates do not belong to the selected owners: {sorted(missing)}"
        raise ValueError(msg)
    return selected


def _selected_owners(request: ReadRequest, entries: dict[str, Document]) -> dict[str, Document]:
    """拒绝错误库和过期身份, 不将旧别名悄悄替换为新材料."""
    result: dict[str, Document] = {}
    for identifier in request.ids:
        if identifier not in entries or entries[identifier]["kind"] != LIBRARY_KINDS[request.library]:
            msg = f"Unknown {request.library} entry: {identifier}"
            raise ValueError(msg)
        result[identifier] = entries[identifier]
    return result


def select_library(store: LibraryStore, requests: list[ReadRequest]) -> dict[str, Document]:
    """返回顺序稳定且去重的完整选材, 不追踪引用或标记已读.

    Args:
        store: 当前库及其材料视图.
        requests: 按库选中的拥有者和可选候选.

    Returns:
        按请求顺序排列的身份到完整材料映射.

    Raises:
        ValueError: 库、身份或候选归属不合法.
    """
    entries = store.material_entries()
    result: dict[str, Document] = {}
    for request in requests:
        owners = _selected_owners(request, entries)
        result.update(owners)
        if request.include_candidates:
            result.update((identifier, entries[identifier]) for identifier in _selected_candidates(request, owners))
    return result


def _entry_index(entry: Document, library: str, processing_status: dict[str, str]) -> Document:
    """只投影发现身份所需字段, 不生成模型摘要."""
    content = cast("Document", entry["content"])
    identifier = str(entry["handle"])
    result: Document = {"id": identifier, "library": library, "kind": entry["kind"], "status": processing_status.get(identifier, "pending")}
    for field in ("name", "element_ids", "participants"):
        if field in content:
            result[field] = content[field]
    if entry["kind"] == "element":
        result["element_ids"] = [identifier]
    if "owner" in entry:
        result["owner_id"] = entry["owner"]
    result["candidate_count"] = len(cast("list[str]", entry.get("candidate_ids", [])))
    result["reference_count"] = sum(len(cast("list[object]", content.get(field, []))) for field in ("feature_refs", "relation_refs", "requires"))
    return result


def _matches_elements(index: Document, elements: list[str]) -> bool:
    """元素过滤同时覆盖直接拥有者范围和关系的元素端点."""
    if not elements:
        return True
    identifiers = set(cast("list[str]", index.get("element_ids", [])))
    identifiers.update(str(item["id"]) for item in cast("list[Document]", index.get("participants", [])) if item["kind"] == "element")
    return bool(identifiers.intersection(elements))


def _matches_endpoints(index: Document, endpoints: list[str]) -> bool:
    """直接端点过滤不限制元素、特征或关系类型, 不递归解析正文."""
    identifiers = {str(item["id"]) for item in cast("list[Document]", index.get("participants", []))}
    return not endpoints or bool(identifiers.intersection(endpoints))


def _catalog_rows(store: LibraryStore, query: ListLibraryInput, processing_status: dict[str, str]) -> list[Document]:
    """目录遍历独立于正文选择, 候选索引由拥有者范围显式启用."""
    entries = store.material_entries()
    rows: list[Document] = []
    for entry in entries.values():
        owner = entries.get(str(entry.get("owner")))
        kind = str(owner["kind"] if owner else entry["kind"])
        library = next((name for name, value in LIBRARY_KINDS.items() if value == kind), "")
        if (query.library and query.library != library) or bool(query.owner_ids) != (entry["kind"] == "candidate"):
            continue
        if query.owner_ids and entry.get("owner") not in query.owner_ids:
            continue
        row = _entry_index(entry, library, processing_status)
        element_index = _entry_index(owner, library, processing_status) if owner else row
        if not _matches_elements(element_index, query.element_ids) or not _matches_endpoints(element_index, query.endpoint_ids):
            continue
        if not query.statuses or row["status"] in query.statuses:
            rows.append(row)
    return rows


def _page_offset(cursor: str | None, fingerprint: str, total: int, *, allow_start: bool = False) -> int:
    """过期或属于其他查询的游标必须重新开始, 防止分页遗漏."""
    if cursor is None:
        return 0
    try:
        digest, raw_offset = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii").split(":")
        offset = int(raw_offset)
    except (ValueError, UnicodeError) as error:
        msg = "Invalid library cursor"
        raise ValueError(msg) from error
    if digest != fingerprint or not (0 <= offset < total if allow_start else 0 < offset < total):
        msg = "Stale library cursor; restart the index query"
        raise ValueError(msg)
    return offset


def _encode_cursor(fingerprint: str, offset: int) -> str:
    """编码带快照边界的位置, 不暴露完整台账."""
    return base64.urlsafe_b64encode(f"{fingerprint}:{offset}".encode()).decode()


def _page_chars(value: object) -> int:
    """与工具回执一致, 包含 JSON 字段与分隔符开销."""
    return len(json.dumps(value, ensure_ascii=False))


def _bounded_page(
    rows: list[Document], *, fingerprint: str, offset: int, limit: int, max_chars: int, compact: Callable[[Document], Document], metadata: Document
) -> Document:
    """同时限制页数量和完整回执字符数; 单项过大时保留可发现身份."""
    result: list[Document] = []

    def page() -> Document:
        end = offset + len(result)
        return {
            **metadata,
            "entries": result,
            "total": len(rows),
            "remaining": len(rows) - end,
            "next_cursor": _encode_cursor(fingerprint, end) if end < len(rows) else None,
        }

    for row in rows[offset : offset + limit]:
        result.append(row)
        if _page_chars(page()) <= max_chars:
            continue
        if len(result) > 1:
            result.pop()
            break
        result[0] = compact(row)
        if _page_chars(page()) > max_chars:
            msg = "Index identity exceeds page budget; increase max_chars"
            raise ValueError(msg)
    response = page()
    if _page_chars(response) > max_chars:
        msg = "Index metadata exceeds page budget; increase max_chars"
        raise ValueError(msg)
    return response


def _compact_library_row(row: Document) -> Document:
    """只缩短索引字段; 完整判断正文仍由 read_library 提供."""
    keys = ("id", "library", "kind", "status", "owner_id", "candidate_count", "reference_count")
    return {**{key: row[key] for key in keys if key in row}, "details_omitted": True, "details_tool": "read_library"}


def list_library(
    store: LibraryStore,
    *,
    library: LibraryName | None = None,
    element_ids: list[str] | None = None,
    endpoint_ids: list[str] | None = None,
    statuses: list[str] | None = None,
    owner_ids: list[str] | None = None,
    cursor: str | None = None,
    limit: int = 30,
    processing_status: dict[str, str] | None = None,
    max_chars: int = 8000,
) -> Document:
    """返回有版本边界的轻量目录分页, 不推进任何比较进度.

    Args:
        store: 当前完整业务库.
        library: 可选库名, 省略时遍历四库.
        element_ids: 元素范围及直接关系端点过滤.
        endpoint_ids: 关系的直接元素、特征或关系端点过滤.
        statuses: 后端比较状态过滤.
        owner_ids: 非空时仅列出这些拥有者的候选索引.
        cursor: 上页返回的同查询游标, 库或进度变化时失效.
        limit: 本页最多条目数, 范围为 1 至 100.
        processing_status: 后端维护的身份到处理状态映射.
        max_chars: 完整 JSON 回执的最大字符数, 超大索引降为身份与计数.

    Returns:
        当前页、库版本、总数、剩余数及后续游标.

    Raises:
        ValueError: 参数非法或游标过期.
    """
    query = ListLibraryInput(
        library=library,
        element_ids=element_ids or [],
        endpoint_ids=endpoint_ids or [],
        statuses=statuses or [],
        owner_ids=owner_ids or [],
        cursor=cursor,
        limit=limit,
    )
    rows = _catalog_rows(store, query, processing_status or {})
    snapshot = {"revision": store.revision, "query": query.model_dump(exclude={"cursor", "limit"}), "rows": rows}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    offset = _page_offset(cursor, fingerprint, len(rows))
    return _bounded_page(
        rows,
        fingerprint=fingerprint,
        offset=offset,
        limit=limit,
        max_chars=max_chars,
        compact=_compact_library_row,
        metadata={"revision": store.revision},
    )
