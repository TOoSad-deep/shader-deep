"""对待提交副本执行合并, 由库入口统一校验和发布."""

from __future__ import annotations

from typing import cast

from shader_deep.domain.library.documents import SECTIONS, Document, _dump, _index, _objects


def _merge_candidates(data: Document, sources: list[str], target: str) -> None:
    """只允许同一拥有者且前提一致的候选显式归并."""
    entries = _index(data)
    owners = {entries[identifier][2] for identifier in sources}
    requirements = {_dump(entries[identifier][1].get("requires", [])) for identifier in sources}
    if len(owners) != 1 or len(requirements) != 1:
        msg = "Merge owners first; candidates with different prerequisites must remain distinct"
        raise ValueError(msg)
    owner = entries[str(entries[target][2])][1]
    owner["candidates"] = [item for item in _objects(owner["candidates"]) if item["id"] not in sources or item["id"] == target]


def _merge_owners(data: Document, kind: str, sources: list[str], target: str, description: str | None) -> None:
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
