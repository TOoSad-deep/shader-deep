"""完整材料读取、分片及呈现登记, 不解析模型框架消息."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from shader_deep.domain.library.documents import Document, _compact, _dump, _objects

if TYPE_CHECKING:
    from shader_deep.domain.library.store import LibraryStore


def _read_object(store: LibraryStore, identifier: str, entries: dict[str, tuple[str, Document, str | None]] | None = None) -> Document:
    """将候选正文拆成独立完整条目, 拥有者保留完整元数据及候选句柄."""
    kind, item, owner = (entries if entries is not None else store._entries())[identifier]
    content = dict(item)
    candidates = _objects(content.pop("candidates", []))
    result: Document = {"handle": identifier, "kind": kind, "content": _compact(content)}
    if "candidates" in item:
        result["candidate_ids"] = [str(candidate["id"]) for candidate in candidates]
        result["candidate_count"] = len(candidates)
    if owner is not None:
        result["owner"] = owner
    return result


def _chunks(store: LibraryStore, identifier: str, item: Document) -> list[str]:
    """对过大的单个对象编码做无损分段, 正文未完整呈现前不算已读."""
    body = _dump(item)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    key = (identifier, digest)
    if key in store._fragment_groups:
        return sorted(store._fragment_groups[key], key=lambda handle: cast("int", store._fragments[handle]["offset"]))
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
            if len(_dump({**base, "text": body[offset : offset + middle]})) + 300 <= store.max_response_chars:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            msg = "Response budget cannot hold fragment metadata; increase max_response_chars"
            raise ValueError(msg)
        store._fragments[handle] = {**base, "text": body[offset : offset + low]}
        store._fragment_targets[handle] = key
        handles.append(handle)
        offset += low
    store._fragment_groups[key] = set(handles)
    return handles


def _materials(store: LibraryStore, handles: list[str], entries: dict[str, tuple[str, Document, str | None]]) -> list[str]:
    """普通对象保持完整, 过大对象转为可逐页读取的有限片段序列."""
    result: list[str] = []
    for requested in handles:
        if requested in store._fragments:
            result.append(requested)
            continue
        identifier = store.resolve(requested)
        item = store._read_object(identifier, entries)
        if len(_dump(item)) + 300 > store.max_response_chars:
            result.extend(store._chunks(identifier, item))
        else:
            result.append(identifier)
    return list(dict.fromkeys(result))


def _material(store: LibraryStore, handle: str, entries: dict[str, tuple[str, Document, str | None]]) -> Document:
    """取得请求材料, 片段也是独立合法 JSON 对象."""
    return store._fragments[handle] if handle in store._fragments else store._read_object(handle, entries)


def read(store: LibraryStore, identifier: str, tool_call_id: str | None = None) -> str:
    """读取业务闭包; 过大单项以带摘要和偏移的编码片段无损传输."""
    handles = store._continuations.get(identifier)
    if handles is None:
        handles = [identifier] if identifier in store._fragments else store._closure([identifier])
    entries = store._entries()
    handles = store._materials(handles, entries)
    response: Document = {"objects": [], "pending_handles": [], "oversized_handles": []}
    delivered: dict[str, str] = {}
    deferred: list[str] = []
    for handle in handles:
        item = store._material(handle, entries)
        if len(_dump(response)) + len(_dump(item)) + 200 < store.max_response_chars:
            cast("list[object]", response["objects"]).append(item)
            delivered[handle] = _dump(item)
        else:
            deferred.append(handle)
    response["pending_handles"] = deferred
    fragmented = any(handle in store._fragments for handle in delivered)
    response["complete"] = not deferred and not fragmented
    if fragmented:
        response["fragment_encoding"] = "JSON; concatenate text by source_handle and offset. Source is read after all fragments are presented."
    if deferred:
        continuation = f"read-page-{len(store._continuations) + 1}"
        store._continuations[continuation] = deferred
        response["pending_handles"] = [continuation]
    payload = _dump(response)
    if tool_call_id is not None:
        store._pending[tool_call_id] = (payload, delivered)
    return payload


def _accept_fragment(store: LibraryStore, handle: str, body: str, entries: dict[str, tuple[str, Document, str | None]]) -> None:
    """只有当前对象版本全部片段实际呈现, 才授予该对象已读身份."""
    if body != _dump(store._fragments[handle]):
        return
    identifier, digest = store._fragment_targets[handle]
    if identifier not in entries:
        return
    current = hashlib.sha256(_dump(store._read_object(identifier, entries)).encode("utf-8")).hexdigest()
    if current != digest:
        return
    store._presented_fragments.add(handle)
    if store._fragment_groups[(identifier, digest)] <= store._presented_fragments:
        store.present_materials(store.material_versions([identifier]))


def present_tool_results(store: LibraryStore, results: list[tuple[str, object, bool]]) -> int:
    """仅登记原调用 ID 下实际进入请求且未被截断的工具正文."""
    before = len(store.presented)
    entries = store._entries()
    for call_id, content, failed in results:
        if call_id not in store._pending:
            continue
        payload, delivered = store._pending[call_id]
        if failed or content != payload:
            continue
        for identifier, body in delivered.items():
            if identifier in store._fragments:
                store._accept_fragment(identifier, body, entries)
            elif identifier in entries and _dump(store._read_object(identifier, entries)) == body:
                store.present_materials(store.material_versions([identifier]))
        del store._pending[call_id]
    return len(store.presented) - before
