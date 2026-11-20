"""单个原生评审角色与每次请求的固定双图材料."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRetryMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.review.context import review_input, review_message
from shader_deep.agents.review.middleware import ReviewGuard
from shader_deep.domain.review_items import CheckAnswer  # noqa: TC001  # 工具 schema 在运行时解析单项回答.
from shader_deep.domain.visual_review import ReviewSubmission  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.infrastructure.llm.openrouter import is_openrouter
from shader_deep.infrastructure.llm.options import ResolvedModelOptions
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import Runnable
    from pydantic import JsonValue

    from shader_deep.workflows.review.options import ReviewOptions
    from shader_deep.workflows.review.session import ReviewSession

__all__ = ["build_review_agent", "review_input"]


def _check_tools(session: ReviewSession) -> list[BaseTool]:
    @tool
    def record_review_check(answer: CheckAnswer) -> str:
        """登记一个可回答必查项的视觉依据、差异和未评原因. 已登记的内容不可覆盖."""
        return json.dumps(session.record_check(answer), ensure_ascii=False)

    @tool
    def finish_review(summary: str, limitations: tuple[str, ...] = ()) -> str:
        """全部可回答项已登记后提交总体摘要和可选限制, 由程序派生六维结论."""
        return json.dumps(session.finish(summary, limitations), ensure_ascii=False)

    return [record_review_check, finish_review]


def _tools(session: ReviewSession) -> list[BaseTool]:
    @tool
    def submit_review(report: ReviewSubmission) -> str:
        """提交六维状态及差异证据, 校验通过后接受并结束本次评审."""
        return json.dumps(session.submit(report), ensure_ascii=False)

    @tool
    def stop_review(reason: str) -> str:
        """说明无法继续评审的非空原因并结束本次会话."""
        return json.dumps(session.stop(reason), ensure_ascii=False)

    if session.source.request.protocol == "visual_review_v2":
        return [*_check_tools(session), stop_review]
    return [submit_review, stop_review]


class FixedReviewInput(AgentMiddleware[AgentState[object], None]):
    """实际请求中持续携带固定标准和捕获的图像字节."""

    def __init__(self, session: ReviewSession, allowed_tools: frozenset[str], *, transport_guard: bool) -> None:
        """绑定固定材料与响应原文校验开关."""
        self.session, self.allowed_tools, self.transport_guard = session, allowed_tools, transport_guard

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        tools = [item for item in request.tools if getattr(item, "name", None) in self.allowed_tools]
        settings = request.model_settings
        if isinstance(request.model, ChatOpenAI) and is_openrouter(request.model.openai_api_base):
            # bind_tools 在发送前递归关闭对象并将全部属性列入 required.
            settings = {**settings, "strict": True}
        return request.override(messages=[*request.messages, review_message(self.session)], tools=tools, model_settings=settings)

    def _validate(self, response: ModelResponse) -> ModelResponse:
        if not self.transport_guard:
            return response
        return replace(response, result=[validate_analysis_message(item) if isinstance(item, AIMessage) else item for item in response.result])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """同步请求采用同一固定双图与工具白名单."""
        return self._validate(handler(self._prepare(request)))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步请求在执行工具之前完成原文校验."""
        return self._validate(await handler(self._prepare(request)))


def _runtime_metadata(model: BaseChatModel, session: ReviewSession) -> dict[str, JsonValue]:
    identifier = getattr(model, "model_name", None)
    effort = getattr(model, "reasoning_effort", None)
    body = getattr(model, "extra_body", None)
    if isinstance(body, dict) and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort", effort)
    return {
        "model_id": identifier if isinstance(identifier, str) else None,
        "reasoning_effort": effort if isinstance(effort, str) else None,
        "rule_sha256": session.source.criteria.rule_sha256,
        "profile_sha256": session.source.criteria.profile_sha256,
        "profile_id": session.source.criteria.profile_id,
        "profile_version": session.source.criteria.profile_version,
    }


def build_review_agent(model: BaseChatModel, session: ReviewSession, options: ReviewOptions) -> Runnable:
    """装配原生评审图, 模型历史与工具循环交给 SDK.

    Args:
        model: 已创建的模型, 测试可注入无网络实现.
        session: 本轮固定输入与业务终态.
        options: 逻辑调用和传输预算.

    Returns:
        仅可提交评审或记录受阻的原生图.
    """
    session.record_runtime(_runtime_metadata(model, session))
    selected = (
        configure_analysis_model(
            model, ResolvedModelOptions(options.request_timeout_seconds, options.stream_model_responses, options.max_output_tokens)
        )
        if isinstance(model, ChatOpenAI)
        else model
    )
    tools = _tools(session)
    allowed_tools = frozenset(tool.name for tool in tools)
    return create_deep_agent(
        model=selected,
        tools=tools,
        name="visual-reviewer",
        system_prompt=f"{session.source.rule_text}\n\n{session.source.profile_text}",
        backend=StateBackend(),
        subagents=[],
        middleware=[
            ReviewGuard(session, allowed_tools),
            ModelRetryMiddleware(max_retries=options.max_request_retries, retry_on=_retryable, on_failure="error"),
            FixedReviewInput(session, allowed_tools, transport_guard=isinstance(model, ChatOpenAI)),
        ],
    )
