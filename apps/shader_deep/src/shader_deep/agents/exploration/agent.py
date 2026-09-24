"""以独立输入和历史执行探索, 只返回三库及必要的运行反馈."""

from __future__ import annotations

import json
from dataclasses import asdict
from threading import Lock
from typing import TYPE_CHECKING, cast

from langchain.tools import tool

from shader_deep.agents.exploration.context import build_exploration_context
from shader_deep.agents.exploration.contracts import ExplorationOutcome, OutlineIssue
from shader_deep.agents.exploration.prompts import EXPLORATION_PROMPT
from shader_deep.domain.library.models import ExplorationReport, Feature, Relation, Sketch
from shader_deep.domain.library.validation import validate_exploration
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.budgets import check_request
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler, SubmissionReply
from shader_deep.workflows.configuration import resolve_phase_options

if TYPE_CHECKING:
    from _thread import LockType
    from collections.abc import Callable
    from pathlib import Path

    from langchain.agents.middleware import ModelRequest
    from langchain.tools import BaseTool
    from langchain_core.runnables import RunnableConfig
    from pydantic import JsonValue

    from shader_deep.domain.library.models import VisualOutline
    from shader_deep.infrastructure.tracing import EventLog
    from shader_deep.runtime.events import EventCallback
    from shader_deep.workflows.options import AnalysisOptions


def _issue_tool(outline: VisualOutline, issues: list[OutlineIssue], lock: LockType, finished: Callable[[], str | None]) -> BaseTool:
    """问题通道不更改初稿, 同一任务去重记录反馈."""

    @tool
    def report_outline_issue(region: str, description: str, element_ids: list[str]) -> str:
        """反馈初稿遗漏或分类问题, 不结束当前探索也不更改初稿.

        Args:
            region: 原图中涉及的可见区域.
            description: 具体遗漏或分类问题, 不要求主 Agent 采纳某种机制.
            element_ids: 涉及的已有元素 ID; 新发现的遗漏可以为空.
        """
        if not region.strip() or not description.strip():
            msg = "region and description must not be blank"
            raise ValueError(msg)
        unknown = set(element_ids) - {element.id for element in outline.elements}
        if unknown:
            msg = f"Unknown outline elements: {sorted(unknown)}"
            raise ValueError(msg)
        with lock:
            if reply := finished():
                return reply
            issue = OutlineIssue(region=region, description=description, element_ids=tuple(element_ids))
            if issue not in issues:
                issues.append(issue)
        return json.dumps({"status": "issue_recorded"})

    return report_outline_issue


def _stop_tool(stopped: list[str], lock: LockType, finished: Callable[[], str | None]) -> BaseTool:
    """停止和提交共用锁, 防止终态互相覆盖."""

    @tool
    def stop_exploration(reason: str) -> str:
        """不能形成有效报告时说明原因并结束, 不提交空库伪装成功.

        Args:
            reason: 无法继续的具体原因或待主 Agent 解决的缺口.
        """
        if not reason.strip():
            msg = "reason must not be blank"
            raise ValueError(msg)
        with lock:
            if reply := finished():
                return reply
            stopped.append(reason)
        return json.dumps({"status": "stopped", "reason": reason}, ensure_ascii=False)

    return stop_exploration


def _control_feedback(submissions: SubmissionHandler, execution: AnalysisExecution, limit: int) -> dict[str, object] | None:
    """只有当前草稿或最后一次机会需要额外提醒."""
    control: dict[str, object] = {}
    if draft := submissions.snapshot():
        control["submission"] = draft
    remaining = limit - execution.model_calls
    if limit and remaining <= 1:
        control["model_calls_remaining"] = remaining
    return control or None


def _event_callback(event_log: EventLog | None, task_id: str, execution: AnalysisExecution, stopped: list[str]) -> EventCallback | None:
    """主动停止不会伪装成业务完成事件."""
    if event_log is None:
        return None
    emit = event_log.bind(task_id, "exploration", execution)

    def on_event(event: str, details: dict[str, JsonValue]) -> None:
        if event != "task_completed" or not stopped:
            emit(event, details)

    return on_event


def _check_cancelled(should_stop: Callable[[], bool] | None) -> None:
    """只在请求或提交边界协作停止, 不撤销已发布的报告."""
    if should_stop is not None and should_stop():
        msg = "Exploration cancelled by coordinator"
        raise AnalysisLimitError(msg)


def _record_output_budget(on_event: EventCallback | None, options: AnalysisOptions, source: str) -> None:
    """记录有效预算及来源, 不把回退值伪装成用户输入."""
    if on_event is not None:
        on_event("phase_output_budget", {"phase": "worker", "max_output_tokens": options.max_output_tokens, "source": source})


def _check_worker_request(request: ModelRequest, tools: list[BaseTool], options: AnalysisOptions, should_stop: Callable[[], bool] | None) -> None:
    """在登记呈现和发送前使用与客户端一致的阶段预算."""
    _check_cancelled(should_stop)
    check_request(request, tools, options)


def run_exploration(
    user_request: str,
    outline: VisualOutline,
    exploration_direction: str,
    reference_url: str,
    *,
    options: AnalysisOptions,
    config: RunnableConfig,
    task_id: str,
    directory: Path | None = None,
    event_log: EventLog | None = None,
    commit_report: Callable[[ExplorationReport], dict[str, object]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExplorationOutcome:
    """执行一个独立探索, 保留有效报告、初稿问题或明确失败.

    Args:
        user_request: 用户原始要求.
        outline: 派发时冻结的统一视觉初稿.
        exploration_direction: 不含其他报告结论的开放探索问题.
        reference_url: 本轮固定的完整原图数据 URL.
        options: 请求恢复和子任务调用预算.
        config: 追踪元数据与图执行配置.
        task_id: 后端分配的任务身份, 不进入业务输入.
        directory: 本轮制品目录, 用于保存修复草稿.
        event_log: 可选的公共运行事件日志.
        commit_report: 可选的原子入库回调; 返回短回执后才将任务记为成功.
        should_stop: 可选的协作取消检查; 请求与入库前检查, 已成功入库的报告仍保留.

    Returns:
        探索三库、独立执行记录、初稿反馈与失败原因.
    """
    options, output_source = resolve_phase_options(options, "worker")
    submitted: list[ExplorationReport] = []
    receipts: list[str] = []
    issues: list[OutlineIssue] = []
    stopped: list[str] = []
    execution, lock = AnalysisExecution(), Lock()

    def finished() -> str | None:
        if submitted:
            return json.dumps({"status": "already_submitted"})
        return json.dumps({"status": "stopped", "reason": stopped[0]}) if stopped else None

    def submit(arguments: dict[str, object]) -> str:
        report = ExplorationReport(
            sketch_library=cast("tuple[Sketch, ...]", arguments["sketch_library"]),
            feature_library=cast("tuple[Feature, ...]", arguments["feature_library"]),
            relation_library=cast("tuple[Relation, ...]", arguments["relation_library"]),
        )
        validate_exploration(report, outline)
        if submitted:
            if report != submitted[0]:
                msg = "This task already committed a different exploration; its accepted report cannot be replaced"
                raise ValueError(msg)
            return receipts[0]
        _check_cancelled(should_stop)
        try:
            receipt = commit_report(report) if commit_report else {"status": "submitted"}
            response = json.dumps(receipt, ensure_ascii=False)
        except Exception as exc:  # 入库失败保留可重试草稿, 不得提前记录成功.
            msg = f"Exploration commit failed: {type(exc).__name__}: {exc}"
            raise ValueError(msg) from exc
        submitted.append(report)
        receipts.append(response)
        return response

    @tool
    def submit_exploration(sketch_library: tuple[Sketch, ...], feature_library: tuple[Feature, ...], relation_library: tuple[Relation, ...]) -> str:
        """一次提交完整探索三库, 入库成功后结束任务; 不附加总结或元素表.

        Args:
            sketch_library: 本次新增草图, 引用统一元素和本次局部候选 ID.
            feature_library: 本次新增特征及候选.
            relation_library: 本次新增关系及候选.
        """
        report = ExplorationReport(sketch_library=sketch_library, feature_library=feature_library, relation_library=relation_library)
        reply = submissions.handle("submit_exploration", asdict(report))
        return cast("SubmissionReply", reply).content

    # 已提交报告仍经 submit 比较内容, 避免通用 finished 快捷返回掩盖异内容重放.
    submissions = SubmissionHandler(
        task_id,
        submit_exploration,
        submit,
        lambda: json.dumps({"status": "stopped", "reason": stopped[0]}) if stopped else None,
        lock,
        directory=directory,
    )

    on_event = _event_callback(event_log, task_id, execution, stopped)
    _record_output_budget(on_event, options, output_source)
    loop = AnalysisLoop(
        execution,
        options.max_worker_calls,
        lambda: build_exploration_context(
            user_request, outline, exploration_direction, reference_url, control=_control_feedback(submissions, execution, options.max_worker_calls)
        ),
        lambda: bool(submitted or stopped),
        [submit_exploration, _issue_tool(outline, issues, lock, finished), _stop_tool(stopped, lock, finished), submissions.repair_tool()],
        request_retries=options.max_request_retries,
        submission_handler=submissions,
        on_event=on_event,
        role_prompt_only=True,
        before_request=lambda request: _check_worker_request(request, list(loop.tools), options, should_stop),
    )
    try:
        loop.run(configure_analysis_model(build_model(), options), EXPLORATION_PROMPT, config)
    except AnalysisLimitError as exc:
        execution.status, execution.error = "stopped", str(exc)
    except Exception as exc:  # noqa: BLE001  # 单个子任务失败必须返回, 不能吞掉其他并行任务的有效报告.
        execution.status, execution.error = "failed", f"{type(exc).__name__}: {exc}"
    return _outcome(submitted, stopped, issues, execution, on_event)


def _outcome(
    submitted: list[ExplorationReport],
    stopped: list[str],
    issues: list[OutlineIssue],
    execution: AnalysisExecution,
    on_event: EventCallback | None,
) -> ExplorationOutcome:
    """循环结束与业务成功分开, 停止原因不能被循环完成状态覆盖."""
    if submitted:
        execution.status, execution.error = "completed", None
    elif stopped:
        execution.status, execution.error = "stopped", stopped[0]
    elif not execution.error:
        execution.status, execution.error = "failed", "Exploration ended without a valid report"
    if execution.error and on_event:
        on_event("task_stopped" if execution.status == "stopped" else "task_failed", {"message": execution.error})
    return ExplorationOutcome(report=submitted[0] if submitted else None, execution=execution, issues=tuple(issues), error=execution.error)
