"""四库序列化形状、身份索引与引用变换的纯函数."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from shader_deep.domain.library.models import ExplorationReport
Document = dict[str, object]
SECTIONS = {"sketch": "sketch_library", "feature": "feature_library", "relation": "relation_library"}


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
