"""使用独立历史执行一个分析视角, 并返回该任务的结果与执行记录."""

from __future__ import annotations

import json
from dataclasses import asdict
from threading import Lock
from typing import TYPE_CHECKING, cast

from langchain.tools import tool

from shader_deep.compatibility.analysis.context import build_analysis_context
from shader_deep.compatibility.analysis.prompts import LENS_PROMPT
from shader_deep.domain.blackboard import add_result
from shader_deep.domain.legacy import LensReport  # noqa: TC001  # 工具装饰器需要在运行时解析此参数结构.
from shader_deep.domain.tasks import ResultRecord
from shader_deep.domain.validation import validate_lens_visual
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler, SubmissionReply
from shader_deep.workflows.options import AnalysisOptions

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig

    from shader_deep.domain.legacy import VisualDecomposition
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.tracing import EventLog


def run_lens(
    state: BlackboardState,
    task_id: str,
    reference_url: str,
    limit: int,
    config: RunnableConfig,
    *,
    options: AnalysisOptions | None = None,
    image_size: tuple[int, int] | None = None,
    visual_decomposition: VisualDecomposition | None = None,
    focus_element_ids: tuple[str, ...] = (),
    focus_feature_ids: tuple[str, ...] = (),
    directory: Path | None = None,
    event_log: EventLog | None = None,
) -> tuple[ResultRecord, AnalysisExecution]:
    """在预算内执行独立分析任务, 并将失败保留为明确的结果记录.

    Args:
        state: 固定的任务快照; 工作线程不向共享黑板提交修改.
        task_id: 已登记的视角任务标识.
        reference_url: 包含参考 PNG 实际字节的数据 URL.
        limit: 模型调用次数上限, 包含修正报告的调用; 0 表示不限次数.
        config: 追踪元数据和图执行步数限制.
        options: 当前分析会话的网络传输与请求配置.
        image_size: 参考图原始尺寸, 用于提出测量建议.
        visual_decomposition: 派发时固定的视觉拆分, 后续批次修订不改变该输入.
        focus_element_ids: 当前任务明确关注的元素.
        focus_feature_ids: 当前任务明确关注的特征.
        directory: 保存当前任务草稿的本次运行目录.
        event_log: 本次运行共用的简短事件日志.

    Returns:
        子任务结果, 以及由程序维护的执行记录.
    """
    # 列表作为提交工具和结束判定之间的共享槽位; 单个角色实例最多接受一份报告.
    # 它属于本次调用的局部状态, 不与其他视角共享聊天历史或提交状态.
    submitted: list[ResultRecord] = []
    execution = AnalysisExecution()
    options = options or AnalysisOptions()

    def submit(report: LensReport, summary: str) -> str:
        """共用草稿入口持锁后调用原业务校验, 不再次获取任务锁."""
        if not summary.strip():
            msg = "summary must not be blank"
            raise ValueError(msg)
        validate_lens_visual(report, visual_decomposition, image_size=image_size)
        result = ResultRecord(id=f"{task_id}-report", task_id=task_id, status="completed", summary=summary, analysis_detail=report)
        add_result(state, result)  # 针对输入快照校验; 只有协调器负责提交到共享黑板.
        submitted.append(result)
        return json.dumps({"status": "submitted", "result_id": result.id})

    @tool
    def submit_analysis_report(report: LensReport, summary: str) -> str:
        """提交观察与解释, 并保证报告内的证据引用有效.

        Args:
            report: 结构化的独立视角报告.
            summary: 对分析结论及其局限的简要说明.
        """
        reply = submissions.handle("submit_analysis_report", {"report": asdict(report), "summary": summary})
        return cast("SubmissionReply", reply).content

    submissions = SubmissionHandler(
        task_id,
        submit_analysis_report,
        lambda arguments: submit(cast("LensReport", arguments["report"]), cast("str", arguments["summary"])),
        lambda: json.dumps({"status": "already_submitted", "result_id": submitted[0].id}) if submitted else None,
        Lock(),
        directory=directory,
    )
    on_event = event_log.bind(task_id, "lens", execution) if event_log is not None else None
    loop = AnalysisLoop(
        execution,
        limit,
        lambda: build_analysis_context(
            state,
            task_id,
            reference_url,
            limits={"model_calls_remaining": limit - execution.model_calls if limit else None},
            image_size=image_size,
            visual_decomposition=visual_decomposition,
            focus_element_ids=focus_element_ids,
            focus_feature_ids=focus_feature_ids,
            submission=submissions.snapshot(),
        ),
        lambda: bool(submitted),
        [submit_analysis_report, submissions.repair_tool()],
        request_retries=options.max_request_retries,
        submission_handler=submissions,
        on_event=on_event,
    )
    try:
        loop.run(configure_analysis_model(build_model(), options), LENS_PROMPT, config)
    except AnalysisLimitError as exc:
        execution.status, execution.error = "stopped", str(exc)
        if on_event is not None:
            on_event("task_stopped", {"message": execution.error})
    except Exception as exc:  # noqa: BLE001  # 返回当前子任务的失败信息, 同时保留其他子任务报告.
        execution.status, execution.error = "failed", f"{type(exc).__name__}: {exc}"
        if on_event is not None:
            on_event("task_failed", {"message": execution.error})
    if submitted:
        execution.status = "completed"
        return submitted[0], execution
    # 即使没有可用报告也返回 blocked 记录, 主分析 Agent 可以据此保留缺口或安排补充.
    return ResultRecord(
        id=f"{task_id}-failure", task_id=task_id, status="blocked", summary=execution.error or "Worker did not submit a report"
    ), execution
