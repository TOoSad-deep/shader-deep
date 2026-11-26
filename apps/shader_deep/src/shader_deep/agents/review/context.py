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
        if session.source.coverage is not None:
            payload["view_notes"] = (
                "每张 PNG 对应标签所示的采集窗口。请按各自实际入画的范围描述参考和候选。"
                "局部差异可以说明该项要求未满足; 窗口外的对象外形或场景状态仍须相应视图才能判断。"
            )
        payload["answerable_checks"] = [item.model_dump(mode="json", exclude={"unassessed"}) for item in session.checks if not item.unassessed]
        payload["answered_check_ids"] = [answer.check_id for answer in session.check_answers]
        payload["evidence_gap_notes"] = (
            "单项证据不足时填写 unassessed_reason 和 evidence_gap。"
            "现有双图可通过扩窗或精确采样补证时选 insufficient_crop_or_sample。需要补测具体位置时在 sample_region 填共享像素区域。"
            "当前 PNG 缺可比时间帧或背景上下文时选 missing_context_or_frame。"
        )
        answered = session.answer_revisions
        payload["answer_revisions"] = answered
        if session.model_calls == session.options.max_model_calls:
            pending_ids = [item.check_id for item in session.checks if item.unassessed is None and item.check_id not in answered]
            payload["final_call"] = {
                "pending_check_ids": pending_ids,
                "answer_id_fields": [{"check_id": check_id} for check_id in pending_ids],
                "instructions": (
                    "这是最后一次模型调用。调用 finish_review 时, 把 answer_id_fields 中每个对象的 check_id 字段和值原样复制到对应答案,"
                    "补齐判断后放入 answers 数组, 同时提交 summary 和 limitations。仅有一项时也要填写 check_id。已登记齐全时传 answers=[]。"
                    "单项未评按 evidence_gap_notes 提交。整次评审无法继续时调用 stop_review。"
                ),
            }
    evidence = session.source.evidence
    if evidence is not None:
        payload["evidence"] = evidence.model_dump(mode="json", exclude=set() if evidence.samples else {"units", "alpha_background"})
        notes = (
            "采样值由程序从固定 PNG 计算, 表示各自区域在白底合成后的编码 RGB 均值与加权亮度。请结合图像解释差异及影响, 引用已有数值, 不要估造测量。"
            if evidence.samples
            else "本轮没有区域采样。请依据标记的两张图描述视觉表现。"
        )
        if evidence.coverage is not None:
            notes += "coverage.unassessed 由程序登记, 请不要在提交中重复。只评价有覆盖的必查项。"
            if not item_protocol and evidence.coverage.unassessed:
                notes += "同一维度既有可查项又有程序未评项时, 有覆盖的问题须填写 check(对应必查项原文), 由该维度直接引用。"
        payload["evidence_notes"] = notes
    if session.source.workflow_criteria is not None:
        payload["view_notes"] = (
            str(payload["view_notes"])
            + " 图外顶部角色栏 REFERENCE 对应参考, CANDIDATE 对应候选。角色栏和小图右侧留白均为展示标注, 不属于作品或测量区域。"
        )
    content: list[str | dict[str, object]] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    for label, url in session.source.model_images:
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return HumanMessage(content=content)


def review_input(session: ReviewSession) -> dict[str, object]:
    """初始化一次原生评审图的短请求, 固定材料按实际请求注入."""
    return {"messages": [HumanMessage(content=f"按固定范围评审渲染结果: {session.source.criteria.request.target}")]}
