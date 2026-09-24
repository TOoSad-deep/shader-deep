"""统一计算整轮完成义务并保留部分交付."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_result
from shader_deep.domain.tasks import ResultRecord
from shader_deep.workflows.options import MIN_ANALYSIS_TASKS

if TYPE_CHECKING:
    from shader_deep.workflows.state import AnalysisRun

PROTOCOL = "possibility_library_v1"


def finalize_analysis(run: AnalysisRun) -> None:
    """后端交付合法四库, 失败任务与必要工作缺口影响真实终态."""
    if run.summary_result is not None or run.store is None or not run.store.report_ids or len(run.workers) < MIN_ANALYSIS_TASKS:
        return
    uncovered = run._uncovered()
    if uncovered:
        run.gaps.append({"reason": "uncovered_elements", "element_ids": uncovered})
        absent = {
            element.id: ["本轮没有形成覆盖此元素的候选, 需要后续独立探索"]
            for element in run.store.library.elements
            if element.id in uncovered and not element.unresolved
        }
        if absent:
            run.store.update_elements_unresolved(absent)
    missing = [key for key, value in run.workers.items() if value.status != "completed"]
    unresolved = [issue for issue in run.issues if not run._issue_resolved(str(issue["id"]))]
    unfinished = run.comparisons.snapshot()
    partial = (
        run.stop_reason != "running"
        or not run.finish_requested
        or bool(missing or unresolved or run.gaps or run.comparisons.pending or run.comparisons.deferred or run.comparisons.active)
    )
    status = "partial" if partial else "completed"
    result = ResultRecord(
        id=run.directory.name + "-library",
        task_id=run.task_id,
        status=status,
        summary="可能性库已交付; 候选仍需编码与渲染验证",
        analysis_detail=run.store.library,
        analysis_protocol=PROTOCOL,
    )
    run.state = add_result(run.state, result)
    run.summary_result = result
    if run.stop_reason == "running":
        run.stop_reason = status
        run.execution.status = "completed"
    run.last_action = {"status": status, "missing_tasks": missing, "unresolved_issues": unresolved, "integration": unfinished}
    run.save()
