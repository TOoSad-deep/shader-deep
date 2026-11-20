"""从捕获材料构造本轮评审的真实多模态上下文."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from shader_deep.workflows.review.session import ReviewSession


def review_message(session: ReviewSession) -> HumanMessage:
    """构造固定标准与明确标注角色的参考、候选和局部图像."""
    payload: dict[str, object] = {
        "kind": "visual_review_context",
        "criteria": session.source.criteria.model_dump(mode="json", exclude={"checks": True, "request": {"source"}}),
        "reference_size": list(session.source.reference_size),
        "rendered_size": list(session.source.rendered_size),
        "view_notes": "局部任务中的整图用于定位和必要上下文, 范围以固定目标与区域说明为准. 坐标关系不足时登记未评项.",
        "model_calls_remaining": max(0, session.options.max_model_calls - session.model_calls),
    }
    item_protocol = session.source.request.protocol == "visual_review_v2"
    if item_protocol:
        payload["answerable_checks"] = [
            {"check_id": item.check_id, "dimension": item.dimension, "check": item.check} for item in session.checks if not item.unassessed
        ]
        payload["answered_check_ids"] = [answer.check_id for answer in session.check_answers]
    evidence = session.source.evidence
    if evidence is not None:
        payload["evidence"] = evidence.model_dump(mode="json")
        notes = (
            "采样值由程序从固定 PNG 计算, 表示各自区域在白底合成后的编码 RGB 均值与加权亮度。"
            "请结合图像解释差异及影响, 引用已有数值, 不要估造测量。"
            "coverage.unassessed 由程序登记, 请不要在提交中重复。只评价有覆盖的必查项。"
        )
        payload["evidence_notes"] = (
            notes if item_protocol else notes + "同一维度既有可查项又有程序未评项时, 有覆盖的问题须填写 check(对应必查项原文), 由该维度直接引用。"
        )
    content: list[str | dict[str, object]] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    for label, url in session.source.images:
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return HumanMessage(content=content)


def review_input(session: ReviewSession) -> dict[str, object]:
    """初始化一次原生评审图的短请求, 固定材料按实际请求注入."""
    return {"messages": [HumanMessage(content=f"按固定范围评审渲染结果: {session.source.criteria.request.target}")]}
