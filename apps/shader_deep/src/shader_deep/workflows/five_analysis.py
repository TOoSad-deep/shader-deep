"""单元素、单批独立探索与异步受限整合的五库主工作流."""

from __future__ import annotations

import contextvars
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from langchain_core.exceptions import ModelError

from shader_deep.agents.five_analysis.agent import run_exploration, run_integration, run_planning, run_recovery
from shader_deep.agents.five_analysis.contracts import RecoveryDecision, TargetPlan
from shader_deep.agents.main.agent import run_main_agent
from shader_deep.domain.five_libraries import (
    Element,
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
from shader_deep.infrastructure.analysis_logging import log_analysis
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


def _log_task(
    store: TaskStore,
    attempt: Attempt,
    execution: AnalysisExecution,
    message: str,
    *,
    level: int = logging.INFO,
    details: dict[str, JsonValue] | None = None,
) -> None:
    """统一任务日志身份, 保留调用序号并避免记录完整模型请求."""
    log_analysis(
        store.directory,
        message,
        task_id=attempt.task_id,
        attempt_id=attempt.attempt_id,
        model_call=execution.model_calls,
        level=level,
        details=details,
    )


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
        payload = object_value(store.task(task_id)["payload"])
        _log_task(
            store,
            attempt,
            execution,
            "任务执行开始",
            details={"direction": payload.get("direction"), "input_version": attempt.input_version},
        )
        _log_task(store, attempt, execution, "任务固定输入", level=logging.DEBUG, details=payload)
        try:
            run(attempt, execution)
            _require_terminal(store, task_id)
            _log_task(
                store,
                attempt,
                execution,
                "任务执行结束",
                details={"status": store.task(task_id)["status"], "model_calls": execution.model_calls},
            )
        except Exception as error:  # noqa: BLE001  # 保留模型、图执行和写入错误, 按逻辑任务恢复策略收口.
            if store.is_active(attempt):
                store.fail_attempt(attempt, f"{type(error).__name__}: {error}", permanent=not redispatch or _permanent(error))
            _log_task(
                store,
                attempt,
                execution,
                "任务执行失败",
                level=logging.WARNING,
                details={"error": f"{type(error).__name__}: {error}", "status": store.task(task_id)["status"]},
            )


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


def _log_version(store: TaskStore, version: str, libraries: FiveLibraries) -> None:
    """发布后再打印版本和库计数, 不将准备产物描述为已发布."""
    counts = {name: len(getattr(libraries, name)) for name in ("elements", "features", "relations", "mechanisms", "sketches")}
    log_analysis(store.directory, "五库版本已发布", details={"version": version, **counts})
    log_analysis(store.directory, "完整五库版本", level=logging.DEBUG, details={"version": version, "libraries": libraries.model_dump(mode="json")})


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
    _log_version(store, "V0", baseline)
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
        log_analysis(store.directory, "整合未完成, 回退 V0", level=logging.WARNING, details={"error": failure.description})
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
        _log_version(store, "V1", integrated)
    except (ValueError, OSError) as error:
        log_analysis(store.directory, "V1 发布失败, 回退 V0", level=logging.WARNING, details={"error": str(error)})
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
    """异常保留已发布的最佳版本, 未提交草稿不进入包."""
    state = store.snapshot()
    for task_id, task in object_value(state["tasks"]).items():
        if isinstance(task, dict) and task["status"] in {"pending", "running"}:
            store.cancel(task_id, f"流程异常终止: {type(error).__name__}: {error}")
    if "integration_projection" in object_value(state["versions"]):
        return _interrupted_projection(store, error)
    if "V0" not in object_value(state["versions"]) and not _recover_baseline(store):
        return _failed(store, f"分析未形成合法基线: {type(error).__name__}: {error}")
    selected = _best_published_version(store)
    payload = object_value(store.read_version(selected))
    baseline = FiveLibraries.model_validate(payload["libraries"])
    questions = _read_issues(payload["open_questions"])
    gaps = _read_issues(payload["gaps"])
    return FiveAnalysisResult(
        target_element_id=baseline.elements[0].id,
        libraries=baseline,
        open_questions=questions,
        gaps=(*gaps, Issue(refs=(), description=f"流程异常后保留 {selected}: {type(error).__name__}: {error}")),
        status="partial",
        selected_version=selected,
    )


def _best_published_version(store: TaskStore) -> str:
    """V1 提交已经包含合法整合结果, 投影缓存缺失不使版本回退."""
    return "V1" if "V1" in object_value(store.snapshot()["versions"]) else "V0"


def _interrupted_projection(store: TaskStore, error: BaseException) -> FiveAnalysisResult:
    """整合已经终结时主会话失败不能丢弃 V1 或复算原缺口."""
    result = _read_projection(store, "integration_projection")
    return FiveAnalysisResult(
        target_element_id=result.target_element_id,
        libraries=result.libraries,
        open_questions=result.open_questions,
        gaps=(*result.gaps, Issue(refs=(), description=f"持续主 Agent 未完成交付: {type(error).__name__}: {error}")),
        status="partial" if result.libraries is not None else "failed",
        selected_version=result.selected_version,
    )


def _publish_projection(store: TaskStore, result: FiveAnalysisResult) -> None:
    """交付前冻结状态投影; 封存故障后的恢复不重新计算缺口."""
    store.publish_version("final_projection", _projection_payload(result))


def _projection_payload(result: FiveAnalysisResult) -> dict[str, JsonValue]:
    return {
        "target_element_id": result.target_element_id,
        "status": result.status,
        "selected_version": result.selected_version,
        "open_questions": [item.model_dump(mode="json") for item in result.open_questions],
        "gaps": [item.model_dump(mode="json") for item in result.gaps],
    }


def _read_projection(store: TaskStore, version: str = "final_projection") -> FiveAnalysisResult:
    """投影和库正文从同一提交记录中的版本指针读取."""
    return _projection_result(store, object_value(store.read_version(version)))


def _projection_result(store: TaskStore, projection: dict[str, JsonValue]) -> FiveAnalysisResult:
    """主任务结果和交付投影共用同一不可变版本解析."""
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


def _accept_intermediate(store: TaskStore, task_id: str, version: str, payload: dict[str, JsonValue]) -> None:
    """中间动作独立提交, 不把持续主任务提前置为成功."""
    store.register(task_id, version, payload)
    if store.task(task_id)["status"] == "succeeded":
        return
    _submit_subordinate(store, task_id, payload)


def _submit_subordinate(store: TaskStore, task_id: str, payload: dict[str, JsonValue]) -> None:
    """程序侧动作写入失败也终结实例, 避免留下无法重放的 running 记录."""
    attempt = store.start_attempt(task_id)
    try:
        store.submit(attempt, task_id + "-accepted", payload)
    except Exception as error:  # 将 IO 与校验故障收束到原实例后保留原错误.
        if store.is_active(attempt):
            store.fail_attempt(attempt, f"{type(error).__name__}: {error}", permanent=True)
        raise


@dataclass(kw_only=True)
class MainAnalysisActions:
    """主 Agent 调用的业务动作, 状态与恢复只依赖 TaskStore 已提交事实."""

    store: TaskStore
    model: BaseChatModel
    options: AnalysisOptions
    request: str
    reference_url: str
    input_version: str
    attempt: Attempt
    _read_ids: set[str] = field(default_factory=set, init=False)

    def _active(self) -> None:
        if not self.store.is_active(self.attempt):
            msg = "持续主任务已失去执行权限"
            raise AnalysisLimitError(msg)

    def _tasks(self) -> dict[str, JsonValue]:
        return object_value(self.store.snapshot()["tasks"])

    def _plan(self) -> TargetPlan | None:
        task = self._tasks().get("planning")
        if isinstance(task, dict) and task["status"] == "succeeded":
            return TargetPlan.model_validate(self.store.read_result("planning"))
        return None

    def _elements(self) -> tuple[Element, ...]:
        task = self._tasks().get("elements")
        if isinstance(task, dict) and task["status"] == "succeeded":
            result = object_value(self.store.read_result("elements"))
            values = result["elements"]
            if not isinstance(values, list):
                msg = "持久元素目录必须为数组"
                raise TypeError(msg)
            return tuple(Element.model_validate(value) for value in values)
        plan = self._plan()
        return plan.elements if plan is not None else ()

    def submit_elements(self, elements: tuple[Element, ...]) -> dict[str, JsonValue]:
        """校验并持久登记元素, 返回目录回执并继续主会话."""
        self._active()
        identities = [element.id for element in elements]
        if not elements or len(set(identities)) != len(identities) or any(element.feature_ids for element in elements):
            msg = "元素必须非空、身份唯一且 feature_ids 为空"
            raise ValueError(msg)
        registered = self._elements()
        if registered and elements != registered:
            msg = "已接受的元素目录不可改写"
            raise ValueError(msg)
        payload: dict[str, JsonValue] = {"elements": [element.model_dump(mode="json") for element in elements]}
        _accept_intermediate(self.store, "elements", self.input_version, payload)
        return {"status": "accepted", "result_id": "elements", **payload}

    def _freeze_plan(self, target_element_id: str, directions: tuple[str, ...]) -> TargetPlan:
        plan = TargetPlan(elements=self._elements(), target_element_id=target_element_id, directions=directions)
        if len(plan.directions) != min(self.options.max_tasks, 3):
            msg = "探索方向数量必须等于本轮固定配置, 默认三个且最多三个"
            raise ValueError(msg)
        registered = self._plan()
        if registered is not None:
            if plan != registered:
                msg = "单批探索已经冻结, 不能更换目标或方向"
                raise ValueError(msg)
            return registered
        self.store.register("planning", self.input_version, _planning_input(self.request, self.reference_url))
        _submit_subordinate(self.store, "planning", plan.model_dump(mode="json"))
        return plan

    def dispatch_exploration(self, target_element_id: str, directions: tuple[str, ...]) -> dict[str, JsonValue]:
        """冻结唯一批次并等待探索与有限恢复, 重放不会重启终态任务."""
        self._active()
        plan = self._freeze_plan(target_element_id, directions)
        if not self._explorations_settled(plan):
            _explore(self.store, self.model, self.options, plan, self.request, self.reference_url, self.input_version)
        tasks = [self._task_summary(f"exploration-{number}") for number in range(1, len(plan.directions) + 1)]
        return {
            "status": "accepted",
            "target_element_id": plan.target.id,
            "tasks": tasks,
            "result_ids": [task["result_id"] for task in tasks if task["result_id"] is not None],
        }

    def _explorations_settled(self, plan: TargetPlan) -> bool:
        tasks = self._tasks()
        identifiers = tuple(f"exploration-{number}" for number in range(1, len(plan.directions) + 1))
        return all(identity in tasks and object_value(tasks[identity])["status"] not in {"pending", "running"} for identity in identifiers)

    def _task_summary(self, task_id: str) -> dict[str, JsonValue]:
        task = self.store.task(task_id)
        return {"task_id": task_id, "status": task["status"], "error": task["error"], "result_id": task_id if task["status"] == "succeeded" else None}

    def read_analysis_result(self, result_id: str) -> dict[str, JsonValue]:
        """只读取本轮已接受任务或已发布版本, 不接受任意文件路径."""
        self._active()
        state = self.store.snapshot()
        if result_id in object_value(state["versions"]) and result_id in {"V0", "V1"}:
            result = self.store.read_version(result_id)
        elif result_id in self._tasks() and (result_id == "elements" or result_id.startswith("exploration-")):
            result = self.store.read_result(result_id)
        else:
            msg = "结果引用必须指向本轮已接受的元素、探索或五库版本"
            raise ValueError(msg)
        self._read_ids.add(result_id)
        return {"status": "succeeded", "result_id": result_id, "result": result}

    def _settled_reports(self, plan: TargetPlan) -> tuple[dict[str, ExplorationSubmission], tuple[Issue, ...]]:
        tasks = self._tasks()
        identifiers = tuple(f"exploration-{number}" for number in range(1, len(plan.directions) + 1))
        unresolved = [
            identity for identity in identifiers if identity not in tasks or object_value(tasks[identity])["status"] in {"pending", "running"}
        ]
        unresolved.extend(
            task_id
            for task_id, value in tasks.items()
            if task_id.removeprefix("recovery-") in identifiers and object_value(value)["status"] in {"pending", "running"}
        )
        if unresolved:
            msg = f"探索及恢复尚未结束, 不能启动整合: {unresolved}"
            raise ValueError(msg)
        reports, missing = {}, []
        for task_id in identifiers:
            task = object_value(tasks[task_id])
            if task["status"] == "succeeded":
                reports[task_id] = ExplorationSubmission.model_validate(self.store.read_result(task_id))
            else:
                missing.append(Issue(refs=(plan.target.id,), description=f"{task_id} 未完成: {task.get('error', task['status'])}"))
        return reports, tuple(missing)

    def dispatch_integration(self) -> dict[str, JsonValue]:
        """从终态探索准备 V0 后启动独立整合, 缓存结果与 V0 回退."""
        self._active()
        if "integration_projection" in object_value(self.store.snapshot()["versions"]):
            return self._integration_receipt(_read_projection(self.store, "integration_projection"))
        plan = self._plan()
        if plan is None:
            msg = "整合需要已冻结并执行的探索批次"
            raise ValueError(msg)
        reports, missing = self._settled_reports(plan)
        try:
            baseline, questions, gaps = collect_explorations(plan.target, reports)
        except ValueError as error:
            result = _failed(self.store, f"没有合法可用五库基线: {error}", plan.target.id)
        else:
            result = _integrate(
                self.store,
                self.model,
                self.options,
                baseline,
                questions,
                (*gaps, *missing),
                plan.target.id,
                0,
                exploration_mappings(plan.target, reports),
            )
        self.store.publish_version("integration_projection", _projection_payload(result))
        return self._integration_receipt(result)

    @staticmethod
    def _integration_receipt(result: FiveAnalysisResult) -> dict[str, JsonValue]:
        return {**_projection_payload(result), "result_id": result.selected_version}

    def _end_result(self) -> FiveAnalysisResult:
        versions = object_value(self.store.snapshot()["versions"])
        if "integration_projection" in versions:
            return _read_projection(self.store, "integration_projection")
        if "V0" not in versions and not _recover_baseline(self.store):
            plan = self._plan()
            return _failed(self.store, "主 Agent 结束时尚未形成合法五库基线", plan.target.id if plan else None)
        selected = _best_published_version(self.store)
        payload = object_value(self.store.read_version(selected))
        baseline = FiveLibraries.model_validate(payload["libraries"])
        gaps = _read_issues(payload["gaps"])
        integration = self._tasks().get("integration")
        integrated = selected == "V1" and isinstance(integration, dict) and integration["status"] == "succeeded"
        if not integrated:
            gaps = (*gaps, Issue(refs=(), description=f"主 Agent 未完成整合即结束, 保留 {selected}"))
        return FiveAnalysisResult(
            target_element_id=baseline.elements[0].id,
            libraries=baseline,
            status="partial" if gaps else "completed",
            selected_version=selected,
            open_questions=_read_issues(payload["open_questions"]),
            gaps=gaps,
        )

    def finish_analysis(self) -> dict[str, JsonValue]:
        """结束由程序计算真实交付状态, 中间动作不能冒充完成."""
        self._active()
        for task_id, value in self._tasks().items():
            if task_id != "main" and object_value(value)["status"] in {"pending", "running"}:
                self.store.cancel(task_id, "主 Agent 请求结束, 撤销未完成任务权限")
        result = self._end_result()
        projection = _projection_payload(result)
        self.store.submit(self.attempt, "main-final", projection)
        return {"status": "accepted", **projection}

    def allowed_tools(self) -> tuple[str, ...]:
        """权限由持久前置事实提供; 请求级快照由主 Agent 执行层固定."""
        tools = ["submit_elements", "finish_analysis"]
        if self._elements():
            tools.append("dispatch_exploration")
        if self._plan() is not None:
            tools.extend(("read_analysis_result", "dispatch_integration"))
        return tuple(tools)

    def progress(self) -> object:
        """调用扣费和日志不算进展, 只接受业务变化和新回读."""
        state = self.store.snapshot()
        tasks = tuple((task_id, object_value(value)["status"]) for task_id, value in object_value(state["tasks"]).items())
        return tasks, tuple(sorted(object_value(state["versions"]))), tuple(sorted(self._read_ids))

    def context(self) -> dict[str, JsonValue]:
        """从持久事实重建主请求所需目录和任务索引, 不恢复自然语言记忆."""
        plan = self._plan()
        return {
            "registered_elements": [element.model_dump(mode="json") for element in self._elements()],
            "target_element_id": plan.target.id if plan else None,
            "directions": list(plan.directions) if plan else [],
            "tasks": [self._task_summary(task_id) for task_id in self._tasks() if task_id.startswith("exploration-") or task_id == "integration"],
            "available_versions": [name for name in object_value(self.store.snapshot()["versions"]) if name in {"V0", "V1"}],
        }

    def done(self) -> bool:
        """主循环只有真实最终提交后结束."""
        return self.store.task("main")["status"] == "succeeded"


def _main_stages(
    store: TaskStore,
    model: BaseChatModel,
    options: AnalysisOptions,
    request: str,
    reference_url: str,
    input_version: str,
) -> FiveAnalysisResult:
    """同一主 Agent 依据 skill 与工具回执组织流程, 执行器只保留任务恢复."""
    store.register("main", input_version, _planning_input(request, reference_url))

    def run(attempt: Attempt, execution: AnalysisExecution) -> None:
        actions = MainAnalysisActions(
            store=store, model=model, options=options, request=request, reference_url=reference_url, input_version=input_version, attempt=attempt
        )
        run_main_agent(store, attempt, model, options, execution, request, reference_url, actions)

    _execute_task(store, "main", run, recover=lambda task_id: _decide_last_attempt(store, task_id, model, options))
    if store.task("main")["status"] == "succeeded":
        return _projection_result(store, object_value(store.read_result("main")))
    return _exception_result(store, RuntimeError(f"持续主 Agent 未完成: {store.task('main').get('error')}"))


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
        model_name = getattr(selected_model, "model_name", None)
        log_analysis(
            directory,
            "分析运行开始",
            details={
                "model": model_name if isinstance(model_name, str) else type(selected_model).__name__,
                "user_request": user_request,
                "max_tasks": options.max_tasks,
                "max_parallel": options.max_parallel,
                "log_path": str(directory / "analysis.log"),
            },
        )
        # 快速恢复交付投影前仍核对原始业务输入, 不能复用另一张图或要求.
        store.register("planning", input_version, _planning_input(user_request, reference_url))
        interrupted: KeyboardInterrupt | None = None
        try:
            if "final_projection" in object_value(store.snapshot()["versions"]):
                log_analysis(directory, "恢复已有最终交付投影")
                result = _read_projection(store)
            else:
                result = _main_stages(store, selected_model, options, user_request, reference_url, input_version)
        except KeyboardInterrupt as error:
            log_analysis(directory, "分析运行被中断, 保存可用基线", level=logging.WARNING)
            interrupted = error
            result = _exception_result(store, error)
        except Exception as error:  # noqa: BLE001  # 在明确持久基线上交付异常部分结果, 不发布整合暂存内容.
            log_analysis(directory, "分析流程异常", level=logging.ERROR, details={"error": f"{type(error).__name__}: {error}"})
            result = _exception_result(store, error)
        _publish_projection(store, result)
        if on_delivery is not None:
            on_delivery(result, store)
        if not store.snapshot()["sealed"]:
            store.seal()
        log_analysis(
            directory,
            "分析运行结束",
            details={
                "status": result.status,
                "selected_version": result.selected_version,
                "gaps": [item.model_dump(mode="json") for item in result.gaps],
                "open_questions": [item.model_dump(mode="json") for item in result.open_questions],
            },
        )
        if interrupted is not None:
            raise interrupted
        return result
