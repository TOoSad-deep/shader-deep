"""在黑板登记边界校验分析任务归属及跨报告的证据引用."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.analysis.schemas import AnalysisSummary, LensReport

if TYPE_CHECKING:
    from shader_deep.analysis.schemas import SourcedStatement
    from shader_deep.schemas import BlackboardState, ResultRecord, TaskRecord


def validate_evidence(state: BlackboardState, task: TaskRecord, identifiers: tuple[str, ...], *, measured: bool = False) -> None:
    """校验证据归属、任务可见范围及数值依据要求.

    Args:
        state: 保存程序生成的测量记录的黑板.
        task: 正在选择或引用证据的任务.
        identifiers: 明确指定的证据 ID.
        measured: 当前结论是否声明受到数值测量支持.
    """
    records = state.get("measurements", {})
    parent = task.parent_task_id or task.id
    for identifier in identifiers:
        item = records.get(identifier)
        if item is None or item.parent_task_id != parent or item.target_version != task.target_version:
            msg = f"Evidence is outside analysis: {identifier}"
            raise ValueError(msg)
        if task.lens_config is not None and identifier not in task.evidence_ids:
            msg = f"Evidence was not provided to this task: {identifier}"
            raise ValueError(msg)
    # 裁剪虽由程序生成, 但只增加可查看的图像材料; 声明数值支持必须引用统计或剖面.
    if measured and not any(records[identifier].spec.kind != "crop" for identifier in identifiers):
        msg = "measurement_supported requires a numeric measurement reference; a crop is visual evidence"
        raise ValueError(msg)


def validate_analysis_task(state: BlackboardState, task: TaskRecord) -> None:
    """校验视角子任务是否归属于同一目标下的主分析任务.

    Args:
        state: 包含父任务与明确选入的历史结果的黑板.
        task: 准备登记的任务记录.
    """
    if task.parent_task_id is None and task.lens_config is None:
        if task.evidence_ids:
            msg = "Root tasks receive measurements through their analysis session"
            raise ValueError(msg)
        return
    parent = state["tasks"].get(task.parent_task_id or "")
    if (
        task.role != "analysis"
        or task.lens_config is None
        or parent is None
        or parent.role != "analysis"
        or parent.lens_config is not None
        or parent.target_version != task.target_version
    ):
        msg = "Lens tasks require a root analysis parent with the same target"
        raise ValueError(msg)
    validate_evidence(state, task, task.evidence_ids)
    for identifier in task.related_result_ids:
        result = state["results"][identifier]
        source = state["tasks"][result.task_id]
        if source.parent_task_id != parent.id or not isinstance(result.analysis_detail, LensReport):
            msg = f"Analysis input is outside parent task: {identifier}"
            raise ValueError(msg)


def _validate_sources(state: BlackboardState, detail: AnalysisSummary, statement: SourcedStatement, *, observed: bool) -> None:
    """核对综合陈述的来源; 关键观察必须精确引用报告内的观察条目."""
    for reference in statement.source_refs:
        if reference.result_id not in detail.source_result_ids:
            msg = f"Undeclared source result: {reference.result_id}"
            raise ValueError(msg)
        report = state["results"][reference.result_id].analysis_detail
        if not isinstance(report, LensReport):
            msg = "Summary sources must be lens reports"
            raise TypeError(msg)
        # 关键观察不能引用整份报告或某个解释来充当事实; 其他综合项允许引用解释.
        items = report.observations if observed else (*report.observations, *report.interpretations)
        if (reference.item_id is None and observed) or (reference.item_id is not None and reference.item_id not in {item.id for item in items}):
            msg = f"Invalid {'observation' if observed else 'item'} reference: {reference.result_id}/{reference.item_id}"
            raise ValueError(msg)


def _validate_summary(state: BlackboardState, task: TaskRecord, detail: AnalysisSummary) -> None:
    """逐层核对来源报告、陈述条目、数值证据和缺失任务的归属."""
    if not detail.source_result_ids or len(set(detail.source_result_ids)) != len(detail.source_result_ids):
        msg = "Summary source IDs must be nonempty and unique"
        raise ValueError(msg)
    for identifier in detail.source_result_ids:
        source = state["results"][identifier]
        if state["tasks"][source.task_id].parent_task_id != task.id or not isinstance(source.analysis_detail, LensReport):
            msg = f"Summary source is outside analysis: {identifier}"
            raise ValueError(msg)
    for statement in detail.key_observations:
        _validate_sources(state, detail, statement, observed=True)
        validate_evidence(state, task, statement.evidence_ids, measured=statement.basis == "measurement_supported")
    for statement in (*detail.relationships, *detail.hypotheses, *detail.disagreements, *detail.open_questions, *detail.implementation_hints):
        _validate_sources(state, detail, statement, observed=False)
        validate_evidence(state, task, statement.evidence_ids, measured=statement.basis == "measurement_supported")
    for identifier in detail.missing_task_ids:
        if state["tasks"][identifier].parent_task_id != task.id:
            msg = f"Missing task is outside analysis: {identifier}"
            raise ValueError(msg)


def validate_analysis_result(state: BlackboardState, result: ResultRecord, task: TaskRecord) -> None:
    """校验报告归属, 以及综合陈述的来源链路.

    Args:
        state: 包含全部被引用记录的黑板.
        result: 准备登记的结果, 可附带结构化分析内容.
        task: 该结果的来源任务.
    """
    detail = result.analysis_detail
    if detail is None:
        return
    # 结构化分析内容只写入 analysis_detail, 避免同一结论在通用字段中另存一份而相互矛盾.
    if task.role != "analysis" or result.observations or result.hypotheses or result.limitations or result.recommendation:
        msg = "Analysis content belongs only in analysis_detail on an analysis task"
        raise ValueError(msg)
    if isinstance(detail, LensReport):
        if task.lens_config is None or task.parent_task_id is None:
            msg = "Lens reports require a lens task"
            raise ValueError(msg)
        for observation in detail.observations:
            validate_evidence(state, task, observation.evidence_ids, measured=observation.basis == "measurement_supported")
    elif isinstance(detail, AnalysisSummary) and task.lens_config is None and task.parent_task_id is None:
        _validate_summary(state, task, detail)
    else:
        msg = "Analysis summaries require a root analysis task"
        raise ValueError(msg)
