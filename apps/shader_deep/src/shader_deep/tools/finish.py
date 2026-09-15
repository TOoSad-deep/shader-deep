"""检查候选是否可选择, 保存自检结论并结束生成任务."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from shader_deep.blackboard import add_result
from shader_deep.schemas import ResultRecord

if TYPE_CHECKING:
    from shader_deep.tools.session import RenderSession


def select_candidate(session: RenderSession, candidate_id: str, assessment: str) -> str:
    """在会话的渲染线程中选择已展示的成功候选.

    Args:
        session: 持有候选和预览展示记录的当前会话.
        candidate_id: 本轮待选择的候选标识.
        assessment: 模型对照参考图的自检结论.

    Returns:
        JSON 格式的选择结果或不可选择的原因.
    """
    # 结束操作可重复调用, 但首次选定后不再切换候选或重复登记 selection 结果.
    if session.selected is not None:
        return json.dumps({"status": "already_finished", "candidate_id": session.selected.id})
    # 两个集合仅含本次会话的候选; 历史基线即使有图, 也不能直接作为本轮完成版本.
    # 检查预览已提供, 不等同于机器证明模型认真比较过图片.
    if candidate_id not in session.successful or candidate_id not in session.presented:
        return json.dumps({"status": "invalid_selection", "message": "只能选择本轮已成功渲染且已收到预览的候选"})
    if not assessment.strip():
        # 要求模型留下非空自检结论; 当前不对文字结论做真实性或相似度自动判定.
        return json.dumps({"status": "invalid_selection", "message": "请说明与参考图的差异"})
    # 选择只改变运行状态, 不改任务原始 baseline_id; 后续任务需显式绑定新基线.
    session.selected = session.state["candidates"][candidate_id]
    session.stop_reason = "completed"
    session.state = add_result(
        session.state,
        ResultRecord(
            id=f"{session.run_dir.name}-selection",
            task_id=session.task_id,
            status="completed",
            summary=assessment,
            candidate_ids=(candidate_id,),
            recommendation="本轮生成自检完成, 待用户验收",
        ),
    )
    session.save()
    return json.dumps({"status": "completed", "candidate_id": candidate_id})
