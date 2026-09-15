"""黑板记录的登记、引用校验和读取, 更新函数返回新状态而不修改输入."""

# 黑板只关心记录之间的关系, 不启动模型、读取 PNG 或决定候选是否被采用.
# 调用方使用 state = add_*(state, record) 接收新版本; 旧 state 仍可供回溯.
# 这些函数没有跨会话的合并或锁, 多个 Agent 的写入协调需要由调用方负责.

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.analysis.validation import validate_analysis_result, validate_analysis_task
from shader_deep.schemas import TaskRecords

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shader_deep.schemas import BlackboardState, CandidateRecord, ResultRecord, TargetRecord, TaskRecord


def new_blackboard() -> BlackboardState:
    """创建一个项目的空黑板.

    Returns:
        可由后续主图或应用调用方持有的独立业务状态.
    """
    return {"targets": {}, "tasks": {}, "candidates": {}, "results": {}}


def _require_new(records: Mapping[str, object], identifier: str) -> None:
    # 禁止用相同 ID 覆盖旧记录, 避免历史任务引用的内容被悄悄替换.
    if identifier in records:
        msg = f"Record already exists: {identifier}"
        raise ValueError(msg)


def add_target(state: BlackboardState, target: TargetRecord) -> BlackboardState:
    """登记一个新的目标版本.

    Args:
        state: 当前黑板.
        target: 要登记的用户目标.

    Returns:
        含新目标的黑板, 已有任务继续使用原来绑定的版本.

    Raises:
        ValueError: 目标版本已经存在.
    """
    _require_new(state["targets"], target.version)
    # 外层字典和本次修改的 targets 字典都重新创建; 其余记录继续共享.
    # 这不是 deepcopy, 因此调用方也应通过登记函数更新, 不直接改嵌套字典.
    return {**state, "targets": {**state["targets"], target.version: target}}


def _check_task_inputs(state: BlackboardState, task: TaskRecord) -> None:
    # 先验证引用存在, 通过后才登记任务; 基线不能替代评审任务明确指定的输入.
    if task.target_version not in state["targets"]:
        msg = f"Unknown target version: {task.target_version}"
        raise ValueError(msg)
    if task.role == "review" and not task.candidate_ids:
        msg = "Review task requires at least one candidate input"
        raise ValueError(msg)
    candidates = task.candidate_ids + ((task.baseline_id,) if task.baseline_id is not None else ())
    # 基线和其他候选都需要存在; 不强制它们来自当前目标, 允许重新评审旧版本.
    for identifier in candidates:
        if identifier not in state["candidates"]:
            msg = f"Unknown candidate: {identifier}"
            raise ValueError(msg)
    for identifier in task.related_result_ids:
        if identifier not in state["results"]:
            msg = f"Unknown result: {identifier}"
            raise ValueError(msg)


def add_task(state: BlackboardState, task: TaskRecord) -> BlackboardState:
    """登记任务并核对它明确引用的输入.

    Args:
        state: 当前黑板.
        task: 由调用方指定目标、基线和工作范围的任务.

    Returns:
        含新任务的黑板. 旧候选可被新目标下的任务显式引用, 供重新评价.

    Raises:
        ValueError: 任务已存在、引用不存在或评审任务未指定候选.
    """
    _require_new(state["tasks"], task.id)
    _check_task_inputs(state, task)
    validate_analysis_task(state, task)
    return {**state, "tasks": {**state["tasks"], task.id: task}}


def add_candidate(state: BlackboardState, candidate: CandidateRecord) -> BlackboardState:
    """登记生成任务的一个候选, 保留其来源任务和制品引用.

    Args:
        state: 当前黑板.
        candidate: 本次生成的候选记录.

    Returns:
        含新候选的黑板, 不改变任何任务的起始基线.

    Raises:
        ValueError: 候选已存在或来源任务不是生成角色.
        KeyError: 来源任务不存在.
    """
    _require_new(state["candidates"], candidate.id)
    task = state["tasks"][candidate.task_id]
    # 候选代表生成产物; 分析与评审角色通过 ResultRecord 提交判断.
    if task.role != "generation":
        msg = f"Only generation tasks can register candidates: {task.id}"
        raise ValueError(msg)
    return {**state, "candidates": {**state["candidates"], candidate.id: candidate}}


def _check_result_candidates(state: BlackboardState, result: ResultRecord, task: TaskRecord) -> None:
    # 显式关联的历史结果可以为本任务引入候选证据, 无需复制原候选记录.
    historical_evidence = {identifier for result_id in task.related_result_ids for identifier in state["results"][result_id].candidate_ids}
    for identifier in result.candidate_ids:
        candidate = state["candidates"][identifier]
        # 满足以下任一来源即可引用: 本任务产出、指定输入、起始基线、关联历史证据.
        # 四个条件都不满足, 才说明结果引用了任务范围外的候选.
        if (
            candidate.task_id != task.id
            and identifier not in task.candidate_ids
            and identifier != task.baseline_id
            and identifier not in historical_evidence
        ):
            msg = f"Candidate {identifier} is outside task {task.id}"
            raise ValueError(msg)


def add_result(state: BlackboardState, result: ResultRecord) -> BlackboardState:
    """登记一次任务提交的结果, 保留观察、假设和建议之间的区别.

    Args:
        state: 当前黑板.
        result: 本任务的结果, 可引用指定输入、关联历史结果的候选证据和本任务产出.

    Returns:
        含新结果的黑板. 任务可先提交部分结果, 后续使用新标识补充记录.

    Raises:
        ValueError: 结果标识已存在或引用的候选超出本任务范围.
        KeyError: 来源任务或所引用的候选不存在.
    """
    _require_new(state["results"], result.id)
    task = state["tasks"][result.task_id]
    _check_result_candidates(state, result, task)
    validate_analysis_result(state, result, task)
    return {**state, "results": {**state["results"], result.id: result}}


def read_task(state: BlackboardState, task_id: str) -> TaskRecords:
    """读取本任务绑定的业务输入、已登记产出和结果.

    Args:
        state: 当前黑板.
        task_id: 要读取的任务标识.

    Returns:
        按明确引用解析的记录, 文件内容与模型消息由后续 Context Builder 处理.

    Raises:
        KeyError: 任务或其引用的记录不存在.
    """
    task = state["tasks"][task_id]
    # 输入通过任务中的固定 ID 查找; 产出通过 task_id 收集, 因而新产出下轮可见.
    # 这里只返回记录对象; 代码文本和图片字节由各 Agent 的 Context 按需读取.
    return TaskRecords(
        task=task,
        target=state["targets"][task.target_version],
        baseline=state["candidates"][task.baseline_id] if task.baseline_id is not None else None,
        inputs=tuple(state["candidates"][identifier] for identifier in task.candidate_ids),
        outputs=tuple(candidate for candidate in state["candidates"].values() if candidate.task_id == task_id),
        related_results=tuple(state["results"][identifier] for identifier in task.related_result_ids),
        results=tuple(result for result in state["results"].values() if result.task_id == task_id),
    )
