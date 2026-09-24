"""从真实报告生成可引用目录和假设继承视图, 不引入另一套实体身份."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from shader_deep.domain.legacy import LensReport

if TYPE_CHECKING:
    from shader_deep.domain.legacy import AnalysisSummary, SourceKind
    from shader_deep.domain.tasks import BlackboardState, ResultRecord


class SourceCatalogEntry(TypedDict):
    """单个合法来源及其在原始报告 JSON 中的位置."""

    result_id: str
    item_id: str | None
    kind: SourceKind
    description: str
    pointer: str


def report_catalog(result: ResultRecord) -> list[SourceCatalogEntry]:
    """列出报告自身产出的可引用条目, 不包含绑定初稿或实现草图.

    Args:
        result: 黑板中的真实子报告结果.

    Returns:
        来源身份、真实类型、简述与报告正文中的 JSON Pointer.
    """
    report = result.analysis_detail
    if not isinstance(report, LensReport):
        return []
    entries = [_entry(result.id, None, "report", result.summary, "")]
    for index, observation in enumerate(report.observations):
        entries.append(_entry(result.id, observation.id, "observation", observation.text, f"/observations/{index}"))
    for index, interpretation in enumerate(report.interpretations):
        entries.append(_entry(result.id, interpretation.id, "interpretation", interpretation.text, f"/interpretations/{index}"))
    if report.visual_additions:
        for index, element in enumerate(report.visual_additions.elements):
            entries.append(_entry(result.id, element.id, "visual_element", element.name, f"/visual_additions/elements/{index}"))
        for index, feature in enumerate(report.visual_additions.features):
            entries.append(_entry(result.id, feature.id, "visual_feature", feature.description, f"/visual_additions/features/{index}"))
        for index, relation in enumerate(report.visual_additions.relations):
            entries.append(_entry(result.id, relation.id, "visual_relation", relation.description, f"/visual_additions/relations/{index}"))
    return entries


def _entry(result_id: str, item_id: str | None, kind: SourceKind, description: str, pointer: str) -> SourceCatalogEntry:
    """短说明直接截取现有内容, 不做模型摘要."""
    return {"result_id": result_id, "item_id": item_id, "kind": kind, "description": description[:160], "pointer": pointer}


def unlinked_interpretations(state: BlackboardState, summary: AnalysisSummary) -> list[SourceCatalogEntry]:
    """派生尚未建立继承关系的解释, 不推断它们已经被否定或舍弃.

    Args:
        state: 保存独立子报告的当前黑板.
        summary: 包含可选假设继承关系的综合结果.

    Returns:
        以结果 ID 和局部条目 ID 共同区分的未关联解释目录.
    """
    linked = {(reference.result_id, reference.item_id) for link in summary.hypothesis_links for reference in link.derived_from}
    return [
        entry
        for identifier in summary.source_result_ids
        if identifier in state["results"]
        for entry in report_catalog(state["results"][identifier])
        if entry["kind"] == "interpretation" and (identifier, entry["item_id"]) not in linked
    ]
