"""单个原生规划角色与请求级固定整图材料."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRetryMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.draft_runtime import DraftBackend, DraftToolGuard
from shader_deep.domain.scene_planning import ScenePlanningSubmission  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.options import ResolvedModelOptions
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import Runnable
    from pydantic import JsonValue

    from shader_deep.workflows.planning.options import PlanningOptions
    from shader_deep.workflows.planning.session import PlanningSession

ALLOWED_TOOLS = frozenset({"read_file", "edit_file", "save_planning_draft", "finish_planning", "stop_planning"})
WRITE_TOOLS = frozenset({"edit_file", "save_planning_draft", "finish_planning", "stop_planning"})
SKILL_PATH = "/skills/scene-planner/"
SKILL_FILES = ("SKILL.md", "references/mechanisms.md", "references/alternatives.md", "references/conditions-and-gaps.md")


def _skill_texts() -> dict[str, str]:
    resource = files("shader_deep.resources").joinpath("skills", "scene-planner")
    return {name: resource.joinpath(name).read_text(encoding="utf-8") for name in SKILL_FILES}


def planning_input(session: PlanningSession) -> dict[str, object]:
    """预置只读规则与固定文本, 原图由请求级中间件注入."""
    resources = {SKILL_PATH + name: create_file_data(text) for name, text in _skill_texts().items()}
    source = session.source
    texts = {
        "observations.json": source.observations.model_dump_json(indent=2),
        "observation_manifest.json": source.observation_manifest.model_dump_json(indent=2),
        "generation_constraints.json": source.constraints.model_dump_json(indent=2),
        "request.txt": source.request,
    }
    resources.update({"/inputs/" + name: create_file_data(text) for name, text in texts.items()})
    return {"messages": [HumanMessage(content=source.request)], "files": resources}


def _tools(session: PlanningSession) -> list[BaseTool]:
    @tool
    def save_planning_draft(report: ScenePlanningSubmission) -> str:
        """首次保存完整 M/S 草稿并返回诊断, 后续回读和局部修订."""
        return json.dumps(session.save_draft(report), ensure_ascii=False)

    @tool
    def finish_planning() -> str:
        """接受已完整回读的最新规划草稿, 不接收正文参数."""
        return json.dumps(session.finish_draft(), ensure_ascii=False)

    @tool
    def stop_planning(reason: str) -> str:
        """用非空原因结束受阻规划, 保存当前草稿."""
        return json.dumps(session.stop(reason), ensure_ascii=False)

    return [save_planning_draft, finish_planning, stop_planning]


class FixedPlanningInput(AgentMiddleware[AgentState[object], None]):
    """保持实际主请求中的原图与固定业务材料可见."""

    def __init__(self, session: PlanningSession, *, transport_guard: bool) -> None:
        """绑定本次固定材料及响应校验开关."""
        self.session, self.transport_guard = session, transport_guard

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        self.session.bind_request()
        messages = [*request.messages, _message(dict(self.session.context()), self.session.reference_url)]
        tools = [entry for entry in request.tools if getattr(entry, "name", None) in ALLOWED_TOOLS]
        return request.override(messages=messages, tools=tools)

    def _validated(self, response: ModelResponse) -> ModelResponse:
        if not self.transport_guard:
            return response
        return replace(response, result=[validate_analysis_message(item) if isinstance(item, AIMessage) else item for item in response.result])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """只在实际成功主请求包含正文后更新已见回执."""
        prepared = self._prepare(request)
        response = self._validated(handler(prepared))
        self.session.present_reads(list(prepared.messages))
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步主请求遵循相同的固定材料与回读规则."""
        prepared = self._prepare(request)
        response = self._validated(await handler(prepared))
        self.session.present_reads(list(prepared.messages))
        return response


def _runtime_metadata(model: BaseChatModel, texts: dict[str, str]) -> dict[str, JsonValue]:
    hashes = {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in texts.items()}
    identifier = getattr(model, "model_name", None)
    effort = getattr(model, "reasoning_effort", None)
    body = getattr(model, "extra_body", None)
    if isinstance(body, dict) and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort", effort)
    return {
        "model_id": identifier if isinstance(identifier, str) else None,
        "reasoning_effort": effort if isinstance(effort, str) else None,
        "rule_sha256": hashes["SKILL.md"],
        "resource_sha256": hashes,
        "skill_bundle_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def build_planning_agent(model: BaseChatModel, session: PlanningSession, options: PlanningOptions) -> Runnable:
    """装配一个原生规划图, SDK 管理消息、摘要与工具循环.

    Args:
        model: 已构造的模型, 测试可注入无网络实现.
        session: 本次固定规划会话.
        options: 逻辑调用与传输预算.

    Returns:
        具有受限草稿工具的可调用图.
    """
    texts = _skill_texts()
    session.record_runtime(_runtime_metadata(model, texts))
    selected = (
        configure_analysis_model(
            model, ResolvedModelOptions(options.request_timeout_seconds, options.stream_model_responses, options.max_output_tokens)
        )
        if isinstance(model, ChatOpenAI)
        else model
    )
    guard = DraftToolGuard(
        session,
        allowed_tools=ALLOWED_TOOLS,
        write_tools=WRITE_TOOLS,
        draft_path="/draft/planning.json",
        resource_paths=frozenset(SKILL_PATH + name for name in SKILL_FILES),
        readable_prefixes=("/inputs/", "/conversation_history/", "/large_tool_results/"),
    )
    return create_deep_agent(
        model=selected,
        tools=_tools(session),
        name="scene-planner",
        system_prompt=texts["SKILL.md"],
        backend=CompositeBackend(default=StateBackend(), routes={"/draft/": DraftBackend(session, filename="planning.json")}),
        subagents=[],
        middleware=[
            guard,
            ModelRetryMiddleware(max_retries=options.max_request_retries, retry_on=_retryable, on_failure="error"),
            FixedPlanningInput(session, transport_guard=isinstance(model, ChatOpenAI)),
        ],
    )
