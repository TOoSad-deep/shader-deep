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
        answered = session.answer_revisions
        payload["answer_revisions"] = answered
        if session.model_calls == session.options.max_model_calls:
            payload["final_call"] = {
                "pending_check_ids": [item.check_id for item in session.checks if item.unassessed is None and item.check_id not in answered],
                "instructions": (
                    "这是最后一次模型调用。观察完成时只需调用一次 finish_review: 将每个 pending_check_ids 对应的答案放进 answers,"
                    "同时提交 summary 和 limitations。已登记的答案由程序并入; 没有待答项时传 answers=[]。"
                    "单项证据不足时填写 unassessed_reason。整次评审无法继续时调用 stop_review。"
                ),
            }
    evidence = session.source.evidence
    if evidence is not None:
        coverage_only = item_protocol and not evidence.samples
        payload["evidence"] = evidence.model_dump(mode="json", exclude={"units", "alpha_background"} if coverage_only else set())
        measures = (
            "本轮没有区域采样。请依据标记的两张图描述视觉表现。"
            if coverage_only
            else "采样值由程序从固定 PNG 计算, 表示各自区域在白底合成后的编码 RGB 均值与加权亮度。"
            "请结合图像解释差异及影响, 引用已有数值, 不要估造测量。"
        )
        coverage_note = "coverage.unassessed 由程序登记, 请不要在提交中重复。只评价有覆盖的必查项。"
        notes = measures + (coverage_note if evidence.coverage is not None or not item_protocol else "")
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
