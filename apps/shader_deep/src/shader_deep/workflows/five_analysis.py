"""单元素、单批独立探索与异步受限整合的五库主工作流."""

from __future__ import annotations

import contextvars
import hashlib
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from langchain_core.exceptions import ModelError

from shader_deep.agents.five_analysis.agent import run_exploration, run_integration, run_planning, run_recovery
from shader_deep.agents.five_analysis.contracts import RecoveryDecision, TargetPlan
from shader_deep.domain.five_libraries import (
    ExplorationSubmission,
    FiveLibraries,
    Issue,
    MergeProposal,
    apply_merges,
    collect_explorations,
    exploration_mappings,
    merge_mapping,
    rewrite_issues,
)
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import _retryable
from shader_deep.runtime.budgets import RequestBudgetError
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError
from shader_deep.runtime.task_store import TaskStore
from shader_deep.runtime.task_store.types import integer_value, object_value

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.runtime.task_store import Attempt, JsonValue
    from shader_deep.workflows.options import AnalysisOptions


AUTO_ATTEMPTS = 2


@dataclass(frozen=True, kw_only=True)
class FiveAnalysisResult:
    """交付读取同一选定版本, 失败没有伪造空五库包."""

    target_element_id: str | None
    libraries: FiveLibraries | None
    open_questions: tuple[Issue, ...] = ()
    gaps: tuple[Issue, ...] = ()
    status: Literal["completed", "partial", "failed"]
    selected_version: str | None
    task_state: dict[str, JsonValue] | None = None


def _permanent(error: Exception) -> bool:
    """配置错误及显式调用容量不足不靠重派恢复."""
    return (
        isinstance(error, (RequestBudgetError, AnalysisLimitError, OSError))
        or (isinstance(error, ModelError) and not _retryable(error))
        or ("修复" in str(error) and "耗尽" in str(error))
    )


def _require_terminal(store: TaskStore, task_id: str) -> None:
    if store.task(task_id)["status"] == "running":
        msg = "工作者结束时没有提交合法结果"
        raise RuntimeError(msg)


def _recovery_input(store: TaskStore, task_id: str) -> dict[str, JsonValue]:
    task = store.task(task_id)
    original = object_value(task["payload"])
    attempts = task["attempts"]
    failed_attempt = object_value(attempts[-1]) if isinstance(attempts, list) and attempts else {}
    payload: dict[str, JsonValue] = {
        "original_task_id": task_id,
        "input_version": task["input_version"],
        "failed_attempt_id": failed_attempt.get("attempt_id"),
        "error": task.get("error"),
    }
    for key in ("user_request", "target", "direction", "target_element_id", "scope"):
        if key in original:
            payload[key] = original[key]
    if isinstance(payload.get("target"), dict):
        payload["scope"] = object_value(payload["target"]).get("region")
    return payload


def _decide_last_attempt(store: TaskStore, task_id: str, model: BaseChatModel, options: AnalysisOptions) -> bool:
    """恢复决策是一个独立主角色任务, 自身失败不会再委派恢复."""
    recovery_id = "recovery-" + task_id
    if store.task(task_id)["status"] != "pending":
        return False
    payload = _recovery_input(store, task_id)
    version = str(store.task(task_id)["input_version"])
    store.register(recovery_id, version, payload)
    if store.task(task_id)["status"] != "pending":
        store.cancel(recovery_id, "原任务已结束, 恢复决策不再执行")
        return False
    _execute_task(
        store,
        recovery_id,
        lambda attempt, execution: run_recovery(store, attempt, model, options, execution, payload),
        redispatch=False,
    )
    if store.task(recovery_id)["status"] != "succeeded":
        store.cancel(task_id, f"主恢复决策未完成: {store.task(recovery_id).get('error')}")
        return False
    decision = RecoveryDecision.model_validate(store.read_result(recovery_id))
    if not decision.retry:
        store.cancel(task_id, f"主恢复决定结束: {decision.reason}")
    return decision.retry


def _execute_task(
    store: TaskStore,
    task_id: str,
    run: Callable[[Attempt, AnalysisExecution], None],
    *,
    recover: Callable[[str], bool] | None = None,
    redispatch: bool = True,
) -> None:
    """初次、自动重派、主调度最终重派沿同一身份恢复, 不新增方向."""
    execution = AnalysisExecution(model_calls=integer_value(store.task(task_id).get("model_calls", 0)))
    while store.task(task_id)["status"] == "pending":
        attempts = store.task(task_id)["attempts"]
        if isinstance(attempts, list) and len(attempts) >= AUTO_ATTEMPTS and (recover is None or not recover(task_id)):
            store.cancel(task_id, "主 agent 决定结束任务或恢复决策未成功")
            return
        attempt = store.start_attempt(task_id)
        try:
            run(attempt, execution)
            _require_terminal(store, task_id)
        except Exception as error:  # noqa: BLE001  # 保留模型、图执行和写入错误, 按逻辑任务恢复策略收口.
            if store.is_active(attempt):
                store.fail_attempt(attempt, f"{type(error).__name__}: {error}", permanent=not redispatch or _permanent(error))


def _wait_workers(
    store: TaskStore,
    jobs: dict[str, Callable[[Attempt, AnalysisExecution], None]],
    parallel: int,
    *,
    recover: Callable[[str], bool] | None = None,
) -> None:
    """线程返回只是唤醒信号, 完成以持久状态为准; 每任务复制追踪上下文."""
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="five-analysis") as pool:
        futures = {
            pool.submit(contextvars.copy_context().run, _execute_task, store, task_id, run, recover=recover): task_id for task_id, run in jobs.items()
        }
        pending = set(futures)
        try:
            while pending:
                finished, pending = wait(pending, timeout=0.1)
                for future in finished:
                    # 未预期线程异常保留原异常, 不用无模型响应的轮询掩盖它.
                    future.result()
                for task_id in jobs:
                    store.task(task_id)
        except BaseException:  # 先撤销在途实例的新提交和请求权, 再由线程池等待资源清理.
            for task_id, value in object_value(store.snapshot()["tasks"]).items():
                task = object_value(value)
                if task["status"] in {"pending", "running"}:
                    store.cancel(task_id, "协调器停止等待, 撤销执行权限")
            raise


def _failed(store: TaskStore, description: str, target: str | None = None) -> FiveAnalysisResult:
    return FiveAnalysisResult(
        target_element_id=target,
        libraries=None,
        status="failed",
        selected_version=None,
        gaps=(Issue(refs=(), description=description),),
        task_state=store.snapshot(),
    )


def _planning_input(request: str, reference_url: str) -> dict[str, JsonValue]:
    return {
        "user_request": request,
        "reference_path": "reference.png",
        "reference_fingerprint": hashlib.sha256(reference_url.encode()).hexdigest(),
    }


def _planning(
    store: TaskStore,
    model: BaseChatModel,
    options: AnalysisOptions,
    request: str,
    reference_url: str,
    input_version: str,
) -> tuple[TargetPlan | None, int]:
    store.register("planning", input_version, _planning_input(request, reference_url))
    calls = [0]

    def run(attempt: Attempt, execution: AnalysisExecution) -> None:
        try:
            run_planning(store, attempt, model, options, execution, request, reference_url)
        finally:
            calls[0] = execution.model_calls

    _execute_task(store, "planning", run, recover=lambda task_id: _decide_last_attempt(store, task_id, model, options))
    result = store.read_result("planning") if store.task("planning")["status"] == "succeeded" else None
    return TargetPlan.model_validate(result) if result is not None else None, integer_value(store.task("planning").get("model_calls", calls[0]))


def _explore(
    store: TaskStore,
    model: BaseChatModel,
    options: AnalysisOptions,
    plan: TargetPlan,
    request: str,
    reference_url: str,
    input_version: str,
) -> tuple[dict[str, ExplorationSubmission], tuple[Issue, ...]]:
    jobs: dict[str, Callable[[Attempt, AnalysisExecution], None]] = {}
    for number, direction in enumerate(plan.directions, 1):
        task_id = f"exploration-{number}"
        payload = {
            "user_request": request,
            "reference_path": "reference.png",
            "reference_fingerprint": hashlib.sha256(reference_url.encode()).hexdigest(),
            "target": plan.target.model_dump(mode="json"),
            "direction": direction,
        }
        store.register(task_id, input_version, payload)
        jobs[task_id] = lambda attempt, execution, direction=direction: run_exploration(
            store,
            attempt,
            model,
            options,
            execution,
            request,
            reference_url,
            plan.target,
            direction,
        )
    _wait_workers(store, jobs, min(options.max_parallel, len(jobs)), recover=lambda task_id: _decide_last_attempt(store, task_id, model, options))
    reports, gaps = {}, []
    for task_id in jobs:
        task = store.task(task_id)
        if task["status"] == "succeeded":
            reports[task_id] = ExplorationSubmission.model_validate(store.read_result(task_id))
        else:
            gaps.append(Issue(refs=(plan.target.id,), description=f"{task_id} 未完成: {task.get('error', task['status'])}"))
    return reports, tuple(gaps)


def _integrate(
    store: TaskStore,
    model: BaseChatModel,
    options: AnalysisOptions,
    baseline: FiveLibraries,
    questions: tuple[Issue, ...],
    gaps: tuple[Issue, ...],
    target: str,
    planning_calls: int,
    source_mappings: dict[str, dict[str, str]],
) -> FiveAnalysisResult:
    payload = {
        "source_mappings": source_mappings,
        "libraries": baseline.model_dump(mode="json"),
        "open_questions": [item.model_dump(mode="json") for item in questions],
        "gaps": [item.model_dump(mode="json") for item in gaps],
    }
    baseline_path = store.publish_version("V0", payload)
    store.register(
        "integration",
        "V0",
        {"baseline_path": baseline_path, "target_element_id": target, "scope": baseline.elements[0].region, "source_mappings": source_mappings},
    )
    if options.max_main_calls and planning_calls >= options.max_main_calls:
        store.cancel("integration", "主角色显式调用额度已耗尽")
    else:
        limit = 0  # 主角色共享额度在实际发送前统一核对, 不分配局部剩余额度.
        jobs: dict[str, Callable[[Attempt, AnalysisExecution], None]] = {
            "integration": lambda attempt, execution: run_integration(
                store,
                attempt,
                model,
                options,
                execution,
                baseline,
                target,
                baseline_path,
                limit=limit,
            )
        }
        _wait_workers(store, jobs, 1, recover=lambda task_id: _decide_last_attempt(store, task_id, model, options))
    if store.task("integration")["status"] != "succeeded":
        failure = Issue(refs=(), description=f"整合未完成, 保留 V0: {store.task('integration').get('error')}")
        return FiveAnalysisResult(
            target_element_id=target, libraries=baseline, open_questions=questions, gaps=(*gaps, failure), status="partial", selected_version="V0"
        )
    try:
        proposal = MergeProposal.model_validate(store.read_result("integration"))
        integrated = apply_merges(baseline, proposal, target)
        mapping = merge_mapping(baseline, proposal)
        final_questions, final_gaps = rewrite_issues(questions, mapping), rewrite_issues(gaps, mapping)
        payload = {
            "source_mappings": {
                source: {old: mapping.get(current, current) for old, current in refs.items()} for source, refs in source_mappings.items()
            },
            "libraries": integrated.model_dump(mode="json"),
            "open_questions": [item.model_dump(mode="json") for item in final_questions],
            "gaps": [item.model_dump(mode="json") for item in final_gaps],
        }
        store.publish_version("V1", payload)
    except (ValueError, OSError) as error:
        return FiveAnalysisResult(
            target_element_id=target,
            libraries=baseline,
            open_questions=questions,
            gaps=(*gaps, Issue(refs=(), description=f"V1 发布失败, 保留 V0: {error}")),
            status="partial",
            selected_version="V0",
        )
    return FiveAnalysisResult(
        target_element_id=target,
        libraries=integrated,
        open_questions=final_questions,
        gaps=final_gaps,
        status="partial" if final_gaps else "completed",
        selected_version="V1",
    )


def _read_issues(value: JsonValue) -> tuple[Issue, ...]:
    if not isinstance(value, list):
        msg = "持久版本的问题列表不合法"
        raise TypeError(msg)
    return tuple(Issue.model_validate(item) for item in value)


def _recover_baseline(store: TaskStore) -> bool:
    """异常发生在汇集前时, 只收集已经成功提交的独立任务."""
    tasks = object_value(store.snapshot()["tasks"])
    planning = tasks.get("planning")
    if not isinstance(planning, dict) or planning["status"] != "succeeded":
        return False
    plan = TargetPlan.model_validate(store.read_result("planning"))
    reports: dict[str, ExplorationSubmission] = {}
    missing: list[Issue] = []
    for number in range(1, len(plan.directions) + 1):
        task_id = f"exploration-{number}"
        task = tasks.get(task_id)
        if isinstance(task, dict) and task["status"] == "succeeded":
            reports[task_id] = ExplorationSubmission.model_validate(store.read_result(task_id))
        else:
            missing.append(Issue(refs=(plan.target.id,), description=f"{task_id} 未完成, 保留已提交结果"))
    try:
        baseline, questions, gaps = collect_explorations(plan.target, reports)
    except ValueError:
        return False
    store.publish_version(
        "V0",
        {
            "source_mappings": exploration_mappings(plan.target, reports),
            "libraries": baseline.model_dump(mode="json"),
            "open_questions": [item.model_dump(mode="json") for item in questions],
            "gaps": [item.model_dump(mode="json") for item in (*gaps, *missing)],
        },
    )
    return True


def _exception_result(store: TaskStore, error: BaseException) -> FiveAnalysisResult:
    """已持久 V0 时异常仍交付合法基线, 未提交草稿不进入包."""
    state = store.snapshot()
    for task_id, task in object_value(state["tasks"]).items():
        if isinstance(task, dict) and task["status"] in {"pending", "running"}:
            store.cancel(task_id, f"流程异常终止: {type(error).__name__}: {error}")
    if "V0" not in object_value(state["versions"]) and not _recover_baseline(store):
        return _failed(store, f"分析未形成合法基线: {type(error).__name__}: {error}")
    payload = object_value(store.read_version("V0"))
    baseline = FiveLibraries.model_validate(payload["libraries"])
    questions = _read_issues(payload["open_questions"])
    gaps = _read_issues(payload["gaps"])
    return FiveAnalysisResult(
        target_element_id=baseline.elements[0].id,
        libraries=baseline,
        open_questions=questions,
        gaps=(*gaps, Issue(refs=(), description=f"整合未完成, 流程异常后保留 V0: {type(error).__name__}: {error}")),
        status="partial",
        selected_version="V0",
    )


def _publish_projection(store: TaskStore, result: FiveAnalysisResult) -> None:
    """交付前冻结状态投影; 封存故障后的恢复不重新计算缺口."""
    store.publish_version(
        "final_projection",
        {
            "target_element_id": result.target_element_id,
            "status": result.status,
            "selected_version": result.selected_version,
            "open_questions": [item.model_dump(mode="json") for item in result.open_questions],
            "gaps": [item.model_dump(mode="json") for item in result.gaps],
        },
    )


def _read_projection(store: TaskStore) -> FiveAnalysisResult:
    """投影和库正文从同一提交记录中的版本指针读取."""
    projection = object_value(store.read_version("final_projection"))
    version, target = projection["selected_version"], projection["target_element_id"]
    status = projection["status"]
    if (
        not isinstance(status, str)
        or status not in {"completed", "partial", "failed"}
        or not (version is None or isinstance(version, str))
        or not (target is None or isinstance(target, str))
    ):
        msg = "最终交付投影字段不合法"
        raise ValueError(msg)
    libraries = FiveLibraries.model_validate(object_value(store.read_version(version))["libraries"]) if version is not None else None
    return FiveAnalysisResult(
        target_element_id=target,
        libraries=libraries,
        selected_version=version,
        status=status,
        open_questions=_read_issues(projection["open_questions"]),
        gaps=_read_issues(projection["gaps"]),
    )


def _stages(
    store: TaskStore,
    model: BaseChatModel,
    options: AnalysisOptions,
    request: str,
    reference_url: str,
    input_version: str,
) -> FiveAnalysisResult:
    plan, planning_calls = _planning(store, model, options, request, reference_url, input_version)
    if plan is None:
        return _failed(store, f"目标规划未完成: {store.task('planning').get('error')}")
    reports, missing = _explore(store, model, options, plan, request, reference_url, input_version)
    try:
        baseline, questions, gaps = collect_explorations(plan.target, reports)
    except ValueError as error:
        return _failed(store, f"没有合法可用五库基线: {error}", plan.target.id)
    return _integrate(
        store,
        model,
        options,
        baseline,
        questions,
        (*gaps, *missing),
        plan.target.id,
        planning_calls,
        exploration_mappings(plan.target, reports),
    )


def execute_five_analysis(
    user_request: str,
    reference_url: str,
    options: AnalysisOptions,
    directory: Path,
    *,
    model: BaseChatModel | None = None,
    on_delivery: Callable[[FiveAnalysisResult, TaskStore], None] | None = None,
) -> FiveAnalysisResult:
    """执行真实模型角色链, 将交付回调置于同一协调器锁与封存边界内.

    Args:
        user_request: 本轮用户要求.
        reference_url: 已固定原图的 data URL.
        options: 角色容量、并行度和显式用户调用上限.
        directory: 本轮独立运行目录.
        model: 可注入无网络测试模型; 未提供时沿用环境配置.
        on_delivery: 选定版本后的包发布回调, 不得重新派发任务.

    Returns:
        合法完整或部分五库版本, 或没有可用基线的明确失败结果.
    """
    selected_model = model if model is not None else build_model()
    if not user_request.strip() or not reference_url.startswith("data:image/png;base64,"):
        msg = "五库分析需要非空用户要求与固定 PNG data URL"
        raise ValueError(msg)
    input_version = hashlib.sha256((user_request + reference_url).encode()).hexdigest()
    with TaskStore(directory) as store:
        # 快速恢复交付投影前仍核对原始业务输入, 不能复用另一张图或要求.
        store.register("planning", input_version, _planning_input(user_request, reference_url))
        interrupted: KeyboardInterrupt | None = None
        try:
            if "final_projection" in object_value(store.snapshot()["versions"]):
                result = _read_projection(store)
            else:
                result = _stages(store, selected_model, options, user_request, reference_url, input_version)
        except KeyboardInterrupt as error:
            interrupted = error
            result = _exception_result(store, error)
        except Exception as error:  # noqa: BLE001  # 在明确持久基线上交付异常部分结果, 不发布整合暂存内容.
            result = _exception_result(store, error)
        _publish_projection(store, result)
        if on_delivery is not None:
            on_delivery(result, store)
        if not store.snapshot()["sealed"]:
            store.seal()
        if interrupted is not None:
            raise interrupted
        return result
