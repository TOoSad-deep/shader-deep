"""调参图的执行契约, 同步与异步入口共用同一组检查."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shader_deep.experiments._parameter_state import ParameterProtocolError, current_control, latest_observation

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langchain.agents.middleware import AgentState, ModelRequest, ModelResponse
    from langchain_core.messages import BaseMessage
    from langchain_core.tools import BaseTool
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.runtime import Runtime
    from langgraph.types import Command

    from shader_deep.experiments.parameter_reference import ParameterReference
    from shader_deep.rendering import ShaderPreview

_VERIFICATION_CALLS = 2


def _reference_urls(messages: Sequence[BaseMessage]) -> list[str]:
    urls: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            urls.extend(
                block.get("url") or f"data:{block.get('mime_type')};base64,{block.get('base64')}"
                for block in message.content_blocks
                if block["type"] == "image"
            )
    return urls


class ParameterPolicy(AgentMiddleware):
    """限定本次构建的业务工具, 检查参考身份、版本呈现和收尾状态."""

    def __init__(self, tools: list[BaseTool], preview: ShaderPreview | None, reference: ParameterReference | None) -> None:
        self.allowed = frozenset(tool.name for tool in tools)
        self.preview = preview
        self.reference = reference

    def _validate_binding(self, messages: Sequence[BaseMessage]) -> None:
        if (control := current_control.get()) is not None:
            if self.preview is not control.preview:
                msg = "Runner preview must be the same instance bound to the parameter agent"
                raise ValueError(msg)
            control.bound = True
        if self.reference is not None and any(url != self.reference.data_url for url in _reference_urls(messages)):
            msg = "Task image differs from the frozen parameter reference"
            raise ValueError(msg)

    def before_agent(self, state: AgentState, runtime: Runtime) -> None:
        """在摘要或模型请求前拒绝目标/会话串用."""
        del runtime
        self._validate_binding(state["messages"])

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> None:
        """异步入口在请求前执行相同校验."""
        self.before_agent(state, runtime)

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        control = current_control.get()
        # 本次请求还未计数; 更新后至少需要一次取图调用和一次图像回读请求.
        can_update = control is None or control.remaining > _VERIFICATION_CALLS
        tools = [
            tool
            for tool in request.tools
            if (name := tool.get("name") if isinstance(tool, dict) else tool.name) in self.allowed and (name != "set_parameters" or can_update)
        ]
        return request.override(tools=tools)

    def _check_response(self, request: ModelRequest, response: ModelResponse) -> bool:
        calls = [call for message in response.result if isinstance(message, AIMessage) for call in message.tool_calls]
        if any(call["name"] not in self.allowed for call in calls):
            msg = "Parameter agent requested a tool outside its allowlist"
            raise ParameterProtocolError(msg)
        if len(calls) > 1 and any(call["name"] == "set_parameters" for call in calls):
            msg = "Parameter updates must be the only tool call in a model response"
            raise ParameterProtocolError(msg)
        observed = latest_observation(request.messages)
        if (control := current_control.get()) is not None and observed is not None:
            control.presented_revision = observed.revision
        updated = any(
            isinstance(message, ToolMessage) and message.name == "set_parameters" and message.status != "error" for message in request.messages
        )
        if any(call["name"] == "set_parameters" for call in calls) and observed is None and updated:
            msg = "Capture and present the latest parameter update before updating again"
            raise ParameterProtocolError(msg)
        return not calls

    def _finish(self, request: ModelRequest) -> None:
        if self.preview is None:
            return  # 无预览的建议模式和旧单工具入口不宣称有截图验证.
        observed = latest_observation(request.messages)
        if observed is None:
            msg = "Latest preview image must reach a model request before finishing"
            raise ParameterProtocolError(msg)
        current = self.preview.capture()
        if current != observed:
            msg = "Preview changed after the image presented to the model; capture and inspect it again"
            raise ParameterProtocolError(msg)
        if (control := current_control.get()) is not None:
            control.final_snapshot = current

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """检查完整模型回复, 禁止先执行部分工具再发现越权或并发更新."""
        prepared = self._prepare(request)
        response = handler(prepared)
        if self._check_response(prepared, response):
            self._finish(prepared)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步模型请求使用相同规则, 取图不阻塞事件循环."""
        prepared = self._prepare(request)
        response = await handler(prepared)
        if self._check_response(prepared, response):
            await asyncio.to_thread(self._finish, prepared)
        return response

    def _tool_rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        if call["name"] not in self.allowed:
            msg = "Tool execution outside the parameter allowlist was blocked"
            raise ParameterProtocolError(msg)
        control = current_control.get()
        if call["name"] == "set_parameters" and control is not None and control.remaining < _VERIFICATION_CALLS:
            return ToolMessage(
                content="剩余预算不足以修改后取图并回读. 参数未修改; 请观察当前状态并如实收尾.",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
        return None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """执行前再次核对权限和收尾预算, 不依赖模型遵守工具列表."""
        rejected = self._tool_rejection(request)
        return rejected if rejected is not None else handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        """异步工具执行同样不能绕过权限与预算."""
        rejected = self._tool_rejection(request)
        return rejected if rejected is not None else await handler(request)
