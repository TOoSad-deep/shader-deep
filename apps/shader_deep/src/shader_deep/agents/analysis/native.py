"""原生 Deep Agents 角色与固定输入、task 准入的窄适配."""

# 阅读入口在底部 build_analysis_agent: 主图装配 → _child 三段适配 → 各角色工具.
# 本模块连接 SDK 与业务会话; 哪些提交有效、槽位何时终结仍由 AnalysisSession 判断.

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files
from typing import TYPE_CHECKING, Protocol, cast

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelCallLimitMiddleware, ModelRetryMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI

from shader_deep.agents.analysis.observations import ReferenceObservations
from shader_deep.domain.evidence import ImageRegion  # 工具 schema 在运行时解析类型.
from shader_deep.domain.five_libraries import Element, ExplorationSubmission, MergeProposal  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.options import resolve_role_options
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from deepagents.middleware.subagents import CompiledSubAgent
    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import Runnable
    from langgraph.types import Command
    from pydantic import JsonValue

    from shader_deep.infrastructure.llm.options import AnalysisPhase
    from shader_deep.workflows.options import AnalysisOptions


class AnalysisSession(Protocol):
    """角色只使用业务记录, 不持有模型历史或调度器."""

    directory: Path

    def context(self) -> Mapping[str, object]:
        """返回主角色固定输入与权威进度."""
        ...

    def fixed_input(self, role: str) -> Mapping[str, object]:
        """投影当前角色固定输入."""
        ...

    def submit_elements(self, elements: tuple[Element, ...]) -> Mapping[str, object]:
        """校验并登记元素目录."""
        ...

    def freeze_plan(self, target_element_id: str, directions: tuple[str, ...], target_box: ImageRegion | None = None) -> Mapping[str, object]:
        """冻结唯一目标及本批方向."""
        ...

    def read_analysis_result(self, result_id: str) -> Mapping[str, object]:
        """读取已接受结果正文."""
        ...

    def admit_task(self, role: str) -> bool:
        """原子启动一个合法槽位."""
        ...

    def complete_task(self, role: str) -> None:
        """正常结束未提交时记录失败."""
        ...

    def fail_task(self, role: str, error: str) -> None:
        """记录异常终态并保留已接受结果."""
        ...

    def receipt(self, role: str) -> Mapping[str, object]:
        """返回程序权威任务回执."""
        ...

    def is_active(self, role: str) -> bool:
        """检查当前角色是否仍有提交权限."""
        ...

    def submit_exploration(self, role: str, report: ExplorationSubmission, *, materials: dict[str, JsonValue] | None = None) -> Mapping[str, object]:
        """验证并保存独立探索产物."""
        ...

    def submit_integration(self, proposal: MergeProposal) -> Mapping[str, object]:
        """应用合法合并建议."""
        ...


def _encoded(value: object) -> str:
    """保持模型回执为普通 JSON, 校验错误可在当前框架会话内修正."""
    return json.dumps(value, ensure_ascii=False)


def _business(call: Callable[[], object]) -> str:
    """只把业务校验拒绝交还模型; 非预期异常由运行记录接管."""
    try:
        value = call()
        return value if isinstance(value, str) else _encoded(value)
    except ValueError as error:
        return _encoded({"error": str(error)})


def _skill_files(role: str) -> dict[str, object]:
    """按角色只预置自己的 skill, 不读取父图或其他角色文件."""
    name = "native-orchestration" if role == "main" else "native-integration" if role == "integrator" else "native-exploration"
    content = files("shader_deep.resources").joinpath("skills", name, "SKILL.md").read_text(encoding="utf-8")
    return {f"/skills/{name}/SKILL.md": create_file_data(content)}


def analysis_input(session: AnalysisSession) -> dict[str, object]:
    """构造主图首轮消息与 SDK skill 文件数据."""
    payload = dict(session.context())
    reference = str(payload.pop("reference_url"))
    return {"messages": [_message(payload, reference)], "files": _skill_files("main")}


class FixedInput(AgentMiddleware[AgentState[object], None]):
    """请求级补充固定材料; scratch 和持久消息继续由框架管理."""

    def __init__(
        self,
        context: Callable[[], HumanMessage],
        *,
        child: bool = False,
        presented: Callable[[list[BaseMessage]], None] | None = None,
        transport_guard: bool = False,
    ) -> None:
        """绑定只读材料构造器和实际呈现回调."""
        self.context, self.child, self.presented = context, child, presented
        self.transport_guard = transport_guard

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        # 摘要可能压缩旧消息, 因而每次请求重新附上固定图像与当前业务事实.
        # override 只改变此次模型输入, 不向 AgentState.messages 反复写入同一张图.
        messages = [*request.messages, self.context()]
        if self.presented is not None:
            # 登记的是即将交给 handler 的材料, 不是服务端接收或模型理解的确认.
            self.presented(cast("list[BaseMessage]", messages))
        # 子图既从模型可见工具中移除 task, 也由 TaskAdmission 拒绝实际越权调用.
        tools = [entry for entry in request.tools if not self.child or getattr(entry, "name", None) != "task"]
        return request.override(messages=messages, tools=tools)

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """不把重复图片追加到框架持久历史."""
        return self._validated(handler(self._prepare(request)))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步调用使用同一固定材料投影."""
        return self._validated(await handler(self._prepare(request)))

    def _validated(self, response: ModelResponse) -> ModelResponse:
        if not self.transport_guard:
            return response
        # 在 SDK 执行工具前拒绝不完整的服务响应; 不尝试修补截断参数或猜测模型意图.
        return replace(response, result=[validate_analysis_message(item) if isinstance(item, AIMessage) else item for item in response.result])


class TaskAdmission(AgentMiddleware[AgentState[object], None]):
    """包围 SDK task, 原子准入交给业务会话, 不执行自建循环."""

    def __init__(self, session: AnalysisSession, roles: tuple[str, ...] = ()) -> None:
        """绑定允许槽位, 子图使用空集合拒绝继续委派."""
        self.session, self.roles = session, roles

    def _admit(self, request: ToolCallRequest) -> tuple[str, ToolMessage | None]:
        arguments = request.tool_call["args"]
        role = arguments.get("subagent_type")
        # SDK task 的公开参数只有两个字符串; 无效调用不能提前占用槽位.
        if not isinstance(role, str) or not isinstance(arguments.get("description"), str):
            refusal = _encoded({"error": "task 需要字符串 description 和 subagent_type; 槽位尚未启动"})
            return "", ToolMessage(refusal, tool_call_id=request.tool_call["id"], status="error")
        if role not in self.roles:
            return role, ToolMessage(_encoded({"error": f"未开放的子角色: {role}"}), tool_call_id=request.tool_call["id"])
        try:
            # 冻结计划、一次启动与整合前置条件由会话在同一锁内校验并占槽.
            # 已运行或已结束的槽位只回读现状, 不因模型再次调用而重派.
            if self.session.admit_task(role):
                return role, None
            payload = self.session.receipt(role)
        except ValueError as error:
            payload = {"error": str(error)}
        return role, ToolMessage(_encoded(payload), tool_call_id=request.tool_call["id"])

    def _failed(self, role: str, request: ToolCallRequest, error: Exception) -> ToolMessage:
        self.session.fail_task(role, f"{type(error).__name__}: {error}")
        return ToolMessage(_encoded(self.session.receipt(role)), tool_call_id=request.tool_call["id"])

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """正常、异常与无合法提交均留下终态, 不重派同一方向."""
        if request.tool_call["name"] != "task":
            return handler(request)
        role, refusal = self._admit(request)
        if refusal is not None:
            return refusal
        try:
            return handler(request)
        except Exception as error:  # noqa: BLE001  # SDK 子图异常必须记入该槽位终态, 不重派.
            return self._failed(role, request, error)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """SDK 可并发执行 task, 业务准入仍由同一锁保护."""
        if request.tool_call["name"] != "task":
            return await handler(request)
        role, refusal = self._admit(request)
        if refusal is not None:
            return refusal
        try:
            return await handler(request)
        except Exception as error:  # noqa: BLE001  # SDK 子图异常必须记入该槽位终态, 不重派.
            return self._failed(role, request, error)


def _root_tools(session: AnalysisSession) -> list[BaseTool]:
    """主角色只登记目标和读取权威结果, 委派使用 SDK task."""

    @tool
    def submit_elements(elements: tuple[Element, ...]) -> str:
        """登记原图元素目录, 所有元素 feature_ids 为空."""
        return _business(lambda: session.submit_elements(elements))

    @tool
    def freeze_plan(target_element_id: str, directions: tuple[str, ...], target_box: ImageRegion | None = None) -> str:
        """一次冻结目标和两个或三个方向; target_box 为原图整数框, 右下不含."""
        return _business(lambda: session.freeze_plan(target_element_id, directions, target_box))

    @tool
    def read_analysis_result(result_id: str) -> str:
        """按工具回执中的 result_id 读取已接受结果完整正文."""
        return _business(lambda: session.read_analysis_result(result_id))

    return [submit_elements, freeze_plan, read_analysis_result]


def _explorer_tools(
    session: AnalysisSession, role: str, observations: Callable[[], ReferenceObservations | None], *, enabled: bool
) -> list[BaseTool]:
    """独立探索提交与按需取证, 没有兄弟报告读取能力."""

    @tool
    def submit_exploration(report: ExplorationSubmission) -> str:
        """提交固定目标的完整 F/R/M/S; 校验通过才成为权威结果."""

        def submit() -> object:
            evidence = observations()
            return session.submit_exploration(role, report, materials=evidence.snapshot(report) if evidence else None)

        return _business(submit)

    @tool
    def inspect_reference(kind: str, region: ImageRegion, question: str) -> str:
        """查看当前任务原图区域; kind 为 crop 或 region_stats, 右下边界不含."""
        evidence = observations()
        if evidence is None:
            return _encoded({"error": "当前任务未开放取证"})
        inspect = evidence.tools()[0]
        return _business(lambda: inspect.invoke({"kind": kind, "region": region, "question": question}))

    return [submit_exploration, *([inspect_reference] if enabled else [])]


def _integrator_tools(session: AnalysisSession) -> list[BaseTool]:
    """完整固定 V0 已随实际请求提供, 只提交保留含义的合并建议."""

    @tool
    def submit_merges(proposal: MergeProposal) -> str:
        """只合并既有 F/R/M, 不修改草图; 无安全合并时提交空 groups."""
        return _business(lambda: session.submit_integration(proposal))

    return [submit_merges]


def _model(model: BaseChatModel, options: AnalysisOptions, phase: AnalysisPhase) -> BaseChatModel:
    """保留已有服务、原始参数与推理回传, SDK 内部网络重试保持关闭."""
    return configure_analysis_model(model, resolve_role_options(options, phase)) if isinstance(model, ChatOpenAI) else model


def _middleware(options: AnalysisOptions, limit: int) -> list[AgentMiddleware]:
    """调用限制只计算当前图模型节点, 重试只包围模型请求."""
    # 网络恢复交给这一层; ChatOpenAI 客户端的内部重试已在 _model 配置中关闭.
    # 节点限额不是全运行共享配额, 不合计子图或 SDK 摘要的独立模型请求.
    result: list[AgentMiddleware] = [ModelRetryMiddleware(max_retries=options.max_request_retries, retry_on=_retryable, on_failure="error")]
    if limit:
        result.append(cast("AgentMiddleware", ModelCallLimitMiddleware(run_limit=limit, exit_behavior="end")))
    return result


def _child(model: BaseChatModel, session: AnalysisSession, options: AnalysisOptions, role: str) -> CompiledSubAgent:
    """Runnable 三段只负责固定输入、框架图、权威回执, 丢弃父图状态."""
    evidence: ReferenceObservations | None = None

    def context() -> HumanMessage:
        payload = dict(session.fixed_input(role))
        reference = payload.pop("reference_url", None)
        message = _message(payload, str(reference)) if reference else HumanMessage(content=_encoded(payload))
        return evidence.context(message) if evidence is not None else message

    def project(_state: object) -> dict[str, object]:
        nonlocal evidence
        # SDK isolated 仍可传递 files 等非消息状态, 此处主动丢弃父状态与 task description.
        # 子图只从冻结计划取得本槽位输入, 并预置自己的 skill 文件, 不继承父图 scratch.
        payload = session.fixed_input(role)
        box = payload.get("target_box")
        if role != "integrator" and (options.worker_observation_tools or box is not None):
            evidence = ReferenceObservations(
                str(payload["reference_url"]),
                session.directory / (role + "-evidence"),
                role,
                options.max_measurements,
                target_box=ImageRegion(**cast("dict", box)) if box is not None else None,
                active=lambda: session.is_active(role),
            )
        return {"messages": [context()], "files": _skill_files(role)}

    def receipt(_state: object) -> dict[str, object]:
        # 普通结束不等于提交成功; 无合法提交时补记失败, 已接受的结果则保留.
        session.complete_task(role)
        result = session.receipt(role)
        # 只把权威回执返回父图, 不回传子图消息、todos 或 files.
        # messages 满足 CompiledSubAgent 合同, SDK 优先用 structured_response 生成 task 回执.
        return {"messages": [AIMessage(content=_encoded(result))], "structured_response": result}

    integration = role == "integrator"
    tools = _integrator_tools(session) if integration else _explorer_tools(session, role, lambda: evidence, enabled=options.worker_observation_tools)

    def presented(messages: list[BaseMessage]) -> None:
        if evidence is not None:
            evidence.mark_presented(messages)

    graph = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt="你是整合角色. 按需读取本角色 skill, 比较请求中的固定 V0 完整正文并使用 submit_merges."
        if integration
        else "你是独立探索角色. 按需读取本角色 skill, 依据固定原图、目标和方向观察并使用 submit_exploration.",
        backend=StateBackend(),
        skills=["/skills/"],
        # 空列表不保证移除 SDK 默认 general-purpose; 下方工具过滤与准入共同限制委派.
        subagents=[],
        name=role,
        middleware=[
            *_middleware(options, options.max_integrator_calls if integration else options.max_worker_calls),
            FixedInput(context, child=True, presented=presented, transport_guard=isinstance(model, ChatOpenAI)),
            TaskAdmission(session),
        ],
    )
    # 两侧仅做输入/输出投影, 中间仍由真正的 Deep Agent 执行工具循环和上下文管理.
    runnable = RunnableLambda(project) | graph | RunnableLambda(receipt)
    return {
        "name": role,
        "description": "合并固定 V0 的 F/R/M" if integration else f"独立探索冻结方向槽位 {role}",
        "runnable": runnable,
        "mode": "isolated",
    }


def build_analysis_agent(model: BaseChatModel, session: AnalysisSession, options: AnalysisOptions) -> Runnable:
    """直接装配主 Deep Agent 和本批独立子图.

    Args:
        model: 现有模型客户端, 保留服务配置与传输适配.
        session: 固定输入、提交校验与结果记录.
        options: 分图调用限制、探索数量和取证配置.

    Returns:
        由 SDK 执行工具循环与原生 task 的主图.
    """
    roles = (*tuple(f"explorer-{number}" for number in range(1, options.max_tasks + 1)), "integrator")

    def context() -> HumanMessage:
        payload = dict(session.context())
        payload["max_tasks"] = options.max_tasks
        return _message(payload, str(payload.pop("reference_url")))

    models = {phase: _model(model, options, phase) for phase in ("outline", "worker", "integration")}
    # 每个角色使用独立 StateBackend; scratch/外置工具正文由 SDK 管理, 不作为业务提交.
    return create_deep_agent(
        model=models["outline"],
        tools=_root_tools(session),
        name="main",
        system_prompt="你是分析编排主角色. 按需读取编排 skill, 登记元素并冻结方向; 使用 task 完成本批探索与整合. 只根据业务回执判断完成.",
        backend=StateBackend(),
        skills=["/skills/"],
        subagents=[_child(models["integration" if role == "integrator" else "worker"], session, options, role) for role in roles],
        middleware=[
            *_middleware(options, options.max_orchestrator_calls),
            FixedInput(context, transport_guard=isinstance(model, ChatOpenAI)),
            TaskAdmission(session, roles),
        ],
    )
