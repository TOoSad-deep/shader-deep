"""冻结探索任务的批次调度、受控发布和取消收集."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import asdict
from typing import TYPE_CHECKING, TypeVar, cast

from shader_deep.agents.exploration.agent import run_exploration
from shader_deep.agents.exploration.contracts import ExplorationOutcome
from shader_deep.domain.blackboard import add_result, add_task
from shader_deep.domain.legacy import LensConfig
from shader_deep.domain.tasks import ResultRecord, TaskRecord
from shader_deep.runtime.execution import AnalysisExecution

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from shader_deep.domain.library.models import ExplorationReport, VisualOutline


if TYPE_CHECKING:
    from shader_deep.workflows.state import AnalysisRun
PROTOCOL = "possibility_library_v1"
FutureResult = TypeVar("FutureResult")


def register_tasks(run: AnalysisRun) -> list[TaskRecord]:
    """按冻结方向登记独立任务."""
    tasks = []
    for offset, direction in enumerate(run.directions, start=1):
        identifier = f"{run.directory.name}-a{offset:03d}"
        task = TaskRecord(
            id=identifier,
            role="analysis",
            target_version=run.state["tasks"][run.task_id].target_version,
            objective=direction,
            parent_task_id=run.task_id,
            lens_config=LensConfig(id=identifier + "-direction", origin="generated", name="独立探索", focus=direction),
            analysis_outline=run.outline,
            analysis_purpose="initial",
        )
        run.state = add_task(run.state, task)
        run.workers[task.id] = AnalysisExecution()
        tasks.append(task)
    run.save()
    return tasks


def commit_report(run: AnalysisRun, task: TaskRecord, report: ExplorationReport) -> dict[str, object]:
    """短锁内校验并发布探索报告."""
    # 子任务模型请求期间不持会话锁, 这里只串行发布一次完整提交.
    with run.lock:
        if run.cancelled.is_set():
            msg = "Exploration cancelled before publication"
            raise ValueError(msg)
        identity = task.id + "-report"
        run._require_store().add_report(identity, report)
        if identity not in run.state["results"]:
            run.state = add_result(
                run.state,
                ResultRecord(
                    id=identity,
                    task_id=task.id,
                    status="completed",
                    summary="独立探索已入库",
                    analysis_detail=report,
                    analysis_protocol=PROTOCOL,
                ),
            )
        run.save()
        return {"status": "stored", "result_id": identity}


def collect_result(run: AnalysisRun, task: TaskRecord, outcome: ExplorationOutcome) -> None:
    """收集线程终态和问题, 不重复发布报告."""
    # 成功回执之前已经入库; 收集只合并运行状态和问题, 不重复发布报告.
    with run.lock:
        run.workers[task.id] = outcome.execution
        for item in outcome.issues:
            run.issues.append({"id": str(len(run.issues) + 1), "outline_version": 1, "task_id": task.id, **asdict(item)})
        if task.id + "-report" not in run.state["results"]:
            run.state = add_result(
                run.state,
                ResultRecord(
                    id=task.id + "-failure",
                    task_id=task.id,
                    status="blocked",
                    summary=outcome.error or "未形成可交付候选",
                    analysis_protocol=PROTOCOL,
                ),
            )
        run.save()


def run_batch(run: AnalysisRun) -> None:
    """并行执行探索批次, 取消后仍收集在途结果."""
    tasks = register_tasks(run)
    with ThreadPoolExecutor(max_workers=run.options.max_parallel, thread_name_prefix="exploration") as executor:
        futures = {
            executor.submit(
                copy_context().run,
                run_exploration,
                run.user_request,
                cast("VisualOutline", run.outline),
                task.objective,
                run.reference_url,
                options=run.options,
                config=run.config(task.id),
                directory=run.directory,
                event_log=run.events,
                task_id=task.id,
                commit_report=committer(run, task),
                should_stop=run.cancelled.is_set,
            ): task
            for task in tasks
        }
        collected: set[str] = set()
        try:
            for future in as_completed(futures):
                collect_future(run, futures[future], future)
                collected.add(futures[future].id)
        except KeyboardInterrupt:
            run.cancelled.set()
            with run.lock:
                run.stop_reason, run.execution.status = "interrupted", "stopped"
                run.save()
            for future in futures:
                future.cancel()
            # 在途请求仍受网络超时控制, 返回后取消标记阻止新请求和新提交.
            for future in as_completed(futures):
                if futures[future].id not in collected:
                    collect_future(run, futures[future], future)
            raise


def collect_future(run: AnalysisRun, task: TaskRecord, future: Future[FutureResult]) -> None:
    """将线程异常保存为明确的失败记录."""
    try:
        outcome = cast("ExplorationOutcome", future.result())
    except Exception as exc:  # noqa: BLE001  # 保留单个线程失败, 不丢弃兄弟报告.
        execution = AnalysisExecution(status="stopped" if run.cancelled.is_set() else "failed", error=str(exc))
        outcome = ExplorationOutcome(report=None, execution=execution, issues=(), error=str(exc))
    collect_result(run, task, outcome)


def committer(run: AnalysisRun, task: TaskRecord) -> Callable[[ExplorationReport], dict[str, object]]:
    """为一个已登记任务绑定受控报告提交入口."""

    def commit(report: ExplorationReport) -> dict[str, object]:
        return commit_report(run, task, report)

    return commit
