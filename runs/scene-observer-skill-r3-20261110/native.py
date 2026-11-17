"""单个原生观察角色、请求级固定材料与执行准入."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRetryMiddleware, hook_config
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from shader_deep.domain.scene_observations import SceneObservationSubmission  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.options import ResolvedModelOptions
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.runnables import Runnable
    from langgraph.runtime import Runtime
    from langgraph.types import Command

    from shader_deep.workflows.analysis.session import AnalysisSession
    from shader_deep.workflows.options import AnalysisOptions

ALLOWED_TOOLS = frozenset({"read_file", "submit_scene_observations", "stop_analysis"})
TERMINAL_TOOLS = frozenset({"submit_scene_observations", "stop_analysis"})
SKILL_PATH = "/skills/scene-observer/"
SKILL_FILES = ("SKILL.md", "references/grouping.md", "references/relations.md", "references/uncertainty.md")


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _skill_texts() -> dict[str, str]:
    resource = files("shader_deep.resources").joinpath("skills", "scene-observer")
    return {name: resource.joinpath(name).read_text(encoding="utf-8") for name in SKILL_FILES}


def _resources() -> dict[str, object]:
    return {SKILL_PATH + name: create_file_data(text) for name, text in _skill_texts().items()}


def analysis_input(session: AnalysisSession) -> dict[str, object]:
    """预置可读资源; 原图只由请求级装配注入, 避免首轮重复."""
    return {"messages": [HumanMessage(content=session.request)], "files": _resources()}


def _business(session: AnalysisSession, call: Callable[[], object]) -> str:
    try:
        return _encoded(call())
    except ValueError as error:
        session.record_rejection(validation=True)
        return _encoded({"accepted": False, "error": str(error)})


def _tools(session: AnalysisSession) -> list[BaseTool]:
    @tool
    def submit_scene_observations(report: SceneObservationSubmission) -> str:
        """提交整图上下文、各 E 下的 F 与场景 R; 错误可修正, 合法接受后结束."""
        return _business(session, lambda: session.submit(report))

    @tool
    def stop_analysis(reason: str) -> str:
        """无法提交可靠观察时以非空原因结束, 不登记成功报告."""
        return _business(session, lambda: session.stop(reason))

    return [submit_scene_observations, stop_analysis]


class ObservationGuard(AgentMiddleware[AgentState[object], None]):
    """在摘要包装链之前结束, 同时控制逻辑调用预算和工具准入."""

    def __init__(self, session: AnalysisSession) -> None:
        """绑定本次独立业务会话."""
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """终态先于摘要和计数, 达到预算也不发起额外模型请求."""
        del state, runtime
        return None if self.session.begin_model_call() else {"jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState[object], runtime: Runtime[None]) -> dict[str, object] | None:
        """异步路径使用同一终态门禁."""
        return self.before_model(state, runtime)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call["name"]
        state = cast("Mapping[str, object]", request.state)
        messages = cast("list[BaseMessage]", state.get("messages", []))
        latest = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
        calls = latest.tool_calls if latest is not None else [request.tool_call]
        if len(calls) != 1 and any(call["name"] in TERMINAL_TOOLS for call in calls):
            reason = "提交或停止必须单独调用; 本批全部工具均未执行"
        elif self.session.finished:
            reason = "会话已结束, 不再执行工具"
        elif name not in ALLOWED_TOOLS:
            reason = "本轮只允许 read_file、submit_scene_observations、stop_analysis"
        elif name == "read_file" and not _readable(request.tool_call["args"].get("file_path")):
            reason = "仅可读取本角色规则、样例及本轮 SDK 外置材料"
        else:
            return None
        self.session.record_rejection(validation=False)
        return ToolMessage(content=_encoded({"error": reason}), tool_call_id=request.tool_call["id"], name=name, status="error")

    def _record_result(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        if isinstance(result, ToolMessage) and result.status == "error" and result.name in TERMINAL_TOOLS:
            self.session.record_rejection(validation=True)
        path = request.tool_call["args"].get("file_path")
        if request.tool_call["name"] == "read_file" and isinstance(result, ToolMessage) and path in {SKILL_PATH + name for name in SKILL_FILES}:
            self.session.record_resource_read(str(path), result.status)
        return result

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """在实际 handler 之前拒绝整批冲突或越权操作."""
        refusal = self._refusal(request)
        return refusal if refusal is not None else self._record_result(request, handler(request))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """异步工具并发时也先按完整批次判断."""
        refusal = self._refusal(request)
        return refusal if refusal is not None else self._record_result(request, await handler(request))


def _readable(value: object) -> bool:
    if not isinstance(value, str) or "\\" in value or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value:
        return False
    # StateBackend 为本图隔离的虚拟文件, SDK 非 CompositeBackend 的 artifacts 根为 /.
    return value.startswith((SKILL_PATH, "/conversation_history/", "/large_tool_results/"))


class FixedInput(AgentMiddleware[AgentState[object], None]):
    """只装配实际请求, 不向 SDK 持久历史重复追加原图."""

    def __init__(self, session: AnalysisSession, *, transport_guard: bool) -> None:
        """绑定固定材料及服务响应校验开关."""
        self.session, self.transport_guard = session, transport_guard

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        payload: dict[str, object] = dict(self.session.context())
        messages = [*request.messages, _message(payload, self.session.reference_url)]
        tools = [entry for entry in request.tools if getattr(entry, "name", None) in ALLOWED_TOOLS]
        return request.override(messages=messages, tools=tools)

    def _validated(self, response: ModelResponse) -> ModelResponse:
        if not self.transport_guard:
            return response
        return replace(response, result=[validate_analysis_message(item) if isinstance(item, AIMessage) else item for item in response.result])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """保持每次实际请求中原图与业务上下文可见."""
        return self._validated(handler(self._prepare(request)))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步请求同样注入实际图像内容."""
        return self._validated(await handler(self._prepare(request)))


def build_analysis_agent(model: BaseChatModel, session: AnalysisSession, options: AnalysisOptions) -> Runnable:
    """装配单个 SDK 观察图, 原图与角色能力由程序固定.

    Args:
        model: 已构造的模型, 测试可注入无网络实现.
        session: 本次观察会话.
        options: 通用传输配置与有限模型调用预算.

    Returns:
        由 SDK 管理消息、文件、摘要和工具循环的原生图.
    """
    resources = _skill_texts()
    hashes = {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in resources.items()}
    identifier = getattr(model, "model_name", None)
    session.record_runtime(
        {
            "model_id": identifier if isinstance(identifier, str) else None,
            "rule_sha256": hashes["SKILL.md"],
            "resource_sha256": hashes,
            "skill_bundle_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest(),
        }
    )
    selected = (
        configure_analysis_model(
            model, ResolvedModelOptions(options.request_timeout_seconds, options.stream_model_responses, options.max_output_tokens)
        )
        if isinstance(model, ChatOpenAI)
        else model
    )
    return create_deep_agent(
        model=selected,
        tools=_tools(session),
        name="scene-observer",
        system_prompt=resources["SKILL.md"],
        backend=StateBackend(),
        # 专用角色已激活主流程, 条件式参考通过 read_file 披露; 不再提示重复发现同一 skill.
        subagents=[],
        middleware=[
            ObservationGuard(session),
            ModelRetryMiddleware(max_retries=options.max_request_retries, retry_on=_retryable, on_failure="error"),
            FixedInput(session, transport_guard=isinstance(model, ChatOpenAI)),
        ],
    )
