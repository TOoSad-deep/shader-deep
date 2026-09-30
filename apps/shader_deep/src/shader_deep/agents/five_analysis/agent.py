"""受限五库角色: 规划、独立探索、只读整合分别拥有独立历史."""

from __future__ import annotations

import json
from threading import Lock
from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.five_analysis.audit import RoleAudit
from shader_deep.agents.five_analysis.contracts import RecoveryDecision, TargetPlan  # noqa: TC001  # 工具装饰器运行时解析提交类型.
from shader_deep.agents.five_analysis.loop import FiveAnalysisLoop
from shader_deep.agents.five_analysis.prompts import EXPLORATION_PROMPT, INTEGRATION_PROMPT, PLANNING_PROMPT, RECOVERY_PROMPT
from shader_deep.agents.five_analysis.views import LibraryReader
from shader_deep.domain.five_libraries import ExplorationSubmission, MergeProposal, apply_merges, validate_exploration
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.budgets import check_request
from shader_deep.runtime.execution import AnalysisExecution, AnalysisLimitError
from shader_deep.runtime.task_store.types import integer_value, object_value
from shader_deep.workflows.configuration import resolve_phase_options

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig

    from shader_deep.domain.five_libraries import Element, FiveLibraries
    from shader_deep.runtime.task_store import Attempt, JsonValue, TaskStore
    from shader_deep.workflows.configuration import AnalysisPhase
    from shader_deep.workflows.options import AnalysisOptions


MAIN_CALL_LOCK = Lock()
MAX_RECOVERY_CALLS = 3


def _recovery_parent(store: TaskStore, attempt: Attempt) -> str | None:
    payload = object_value(store.task(attempt.task_id)["payload"])
    parent = payload.get("original_task_id")
    return parent if isinstance(parent, str) else None


def _charge_model(store: TaskStore, attempt: Attempt, options: AnalysisOptions, phase: AnalysisPhase) -> int:
    """所有主角色在同一锁内核对持久总计并消费, 并发恢复不超额."""
    if phase == "worker":
        return store.consume_model_call(attempt, options.max_worker_calls)
    with MAIN_CALL_LOCK:
        tasks = object_value(store.snapshot()["tasks"])
        used = sum(
            integer_value(object_value(value).get("model_calls", 0))
            for task_id, value in tasks.items()
            if task_id in {"planning", "integration"} or task_id.startswith("recovery-")
        )
        if options.max_main_calls and used >= options.max_main_calls:
            msg = "主角色共享的逻辑模型调用额度已耗尽"
            raise AnalysisLimitError(msg)
        return store.consume_model_call(attempt, parent_task_id=_recovery_parent(store, attempt))


def _commit(store: TaskStore, attempt: Attempt, payload: dict[str, JsonValue]) -> str:
    """接受完整业务校验后的提交, 成功回执由持久状态提供."""
    receipt = store.submit(attempt, attempt.attempt_id + "-final", payload, parent_task_id=_recovery_parent(store, attempt))
    return json.dumps({"status": "accepted", "result_path": receipt.result_path}, ensure_ascii=False)


def _control_context(message: HumanMessage, store: TaskStore, attempt: Attempt) -> HumanMessage:
    task = store.task(attempt.task_id)
    records = task.get("attempts", [])
    errors = [record.get("error") for record in records if isinstance(record, dict) and record.get("error")] if isinstance(records, list) else []
    error = task.get("error") or (errors[-1] if errors else None)
    if not error:
        return message
    content = [{"type": "text", "text": message.content}] if isinstance(message.content, str) else list(message.content)
    return message.model_copy(update={"content": [*content, {"type": "text", "text": "本任务恢复反馈: " + str(error)}]})


def _active(store: TaskStore, attempt: Attempt) -> None:
    parent = _recovery_parent(store, attempt)
    parent_active = parent is None or store.task(parent)["status"] == "pending"
    if not store.is_active(attempt) or not parent_active:
        msg = "当前执行实例或原恢复目标已经失去执行权限"
        raise AnalysisLimitError(msg)


def _run(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    prompt: str,
    context: Callable[[], HumanMessage],
    tools: list[BaseTool],
    *,
    phase: AnalysisPhase,
    limit: int,
    reader: LibraryReader | None = None,
) -> None:
    """角色调用复用现有请求检查与网络恢复, 不启用框架额外工具."""
    phase_options, _ = resolve_phase_options(options, phase)
    configured = configure_analysis_model(model, phase_options) if isinstance(model, ChatOpenAI) else model
    audit = RoleAudit(store, attempt, execution)
    loop = FiveAnalysisLoop(
        execution,
        limit,
        lambda: _control_context(context(), store, attempt),
        lambda: store.task(attempt.task_id)["status"] == "succeeded",
        tools,
        request_retries=min(options.max_request_retries, 2),
        role_prompt_only=True,
        before_request=lambda request: check_request(request, tools, phase_options),
        repair_scope=lambda: attempt.task_id,
        progress=lambda: tuple(sorted(reader.returned_ids)) if reader is not None else (),
        on_prepared=reader.mark_presented if reader is not None else None,
        on_event=audit.event,
        on_tool_result=audit.tool,
    )
    loop.repair_charge = lambda: store.consume_repair(attempt)
    loop.model_charge = lambda: _charge_model(store, attempt, options, phase)
    loop.inactive_check = lambda: _active(store, attempt)
    # 有效读取最多为固定快照的对象数; 三轮停滞约束为图执行提供有限界.
    bound = len(reader.index) if reader is not None else 0
    config: RunnableConfig = {
        "run_name": phase,
        "metadata": {"task_id": attempt.task_id, "attempt_id": attempt.attempt_id},
        "recursion_limit": 6 * (bound + 10),
    }
    loop.run(configured, prompt, config)


def run_planning(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    request: str,
    reference_url: str,
) -> None:
    """主角色一次冻结目标和探索批次."""

    @tool
    def submit_target_plan(plan: TargetPlan) -> str:
        """提交元素定位、唯一目标和本批方向; 不接收特征或机制.

        Args:
            plan: 元素的 feature_ids 必须为空, 二到三个开放探索方向.
        """
        if len(plan.directions) != min(options.max_tasks, 3):
            msg = "探索方向数量必须等于本轮固定配置, 默认三个且最多三个"
            raise ValueError(msg)
        return _commit(store, attempt, plan.model_dump(mode="json"))

    _run(
        store,
        attempt,
        model,
        options,
        execution,
        PLANNING_PROMPT,
        lambda: _message({"user_request": request, "default_directions": min(options.max_tasks, 3)}, reference_url),
        [submit_target_plan],
        phase="outline",
        limit=options.max_main_calls,
    )


def run_exploration(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    request: str,
    reference_url: str,
    target: Element,
    direction: str,
) -> None:
    """单个 worker 独立观察原图, 提交范围由领域校验执行."""

    @tool
    def submit_exploration(report: ExplorationSubmission) -> str:
        """提交本目标的独立特征、关系、机制和草图.

        Args:
            report: 本任务完整产物, 未解决内容写入具体 gaps 或 open_questions.
        """
        validate_exploration(report, target)
        return _commit(store, attempt, report.model_dump(mode="json"))

    payload = {"user_request": request, "target_element": target.model_dump(mode="json"), "scope": target.region, "direction": direction}
    _run(
        store,
        attempt,
        model,
        options,
        execution,
        EXPLORATION_PROMPT,
        lambda: _message(payload, reference_url),
        [submit_exploration],
        phase="worker",
        limit=options.max_worker_calls,
    )


def run_integration(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    libraries: FiveLibraries,
    target_id: str,
    baseline_path: str,
    *,
    limit: int = 0,
) -> None:
    """整合按需读取固定 V0, 最终只提交既有 F/R/M 的合并建议."""
    reader = LibraryReader(libraries)

    @tool
    def submit_merges(proposal: MergeProposal) -> str:
        """一次提交全部合并, 冲突时保留原方案并按反馈修复.

        Args:
            proposal: 限 features、relations、mechanisms; keep 必须为组内既有 ID.
        """
        required = {identity for identity, kind in reader.kinds.items() if kind in {"features", "relations", "mechanisms"}}
        unread = required - reader.read_ids
        if unread:
            msg = f"整合完成前必须回读全部 F/R/M 正文, 当前未读: {sorted(unread)}"
            raise ValueError(msg)
        apply_merges(libraries, proposal, target_id)
        return _commit(store, attempt, proposal.model_dump(mode="json"))

    payload = {"input_version": "V0", "baseline_path": baseline_path, "target_element_id": target_id, "directory": reader.directory()}
    _run(
        store,
        attempt,
        model,
        options,
        execution,
        INTEGRATION_PROMPT,
        lambda: HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        [*reader.tools(), submit_merges],
        phase="integration",
        limit=limit,
        reader=reader,
    )


def run_recovery(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    payload: dict[str, JsonValue],
) -> None:
    """主 agent 独立诊断本任务错误, 只提交固定任务是否最后重派."""

    @tool
    def submit_recovery_decision(decision: RecoveryDecision) -> str:
        """允许最后一次重派原任务或结束, 不接受新方向或改写输入.

        Args:
            decision: retry 布尔值和本任务诊断理由, 不含视觉分析产物.
        """
        return _commit(store, attempt, decision.model_dump(mode="json"))

    _run(
        store,
        attempt,
        model,
        options,
        execution,
        RECOVERY_PROMPT,
        lambda: HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        [submit_recovery_decision],
        phase="outline",
        limit=MAX_RECOVERY_CALLS,
    )
