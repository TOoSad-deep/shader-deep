"""实验专用工具边界, 不改变生产 Agent 的默认工具或工作流."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command

ALLOWED_TOOLS = frozenset({"set_parameters", "compare_preview", "get_trial_history"})


class ExperimentTools(AgentMiddleware):
    """从模型可见 Schema 去掉 Deep Agents 内置工具, 拒绝越界响应."""

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """两组使用相同白名单, 完整回复回到图之前检查执行权限."""
        tools = [tool for tool in request.tools if (tool.get("name") if isinstance(tool, dict) else tool.name) in ALLOWED_TOOLS]
        response = handler(request.override(tools=tools))
        for message in response.result:
            if isinstance(message, AIMessage) and any(call["name"] not in ALLOWED_TOOLS for call in message.tool_calls):
                msg = "Model requested a tool outside the frozen experiment allowlist"
                raise RuntimeError(msg)
        return response

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        """执行前再次验证权限, 防止自造工具调用绕过可见 Schema."""
        if request.tool_call["name"] not in ALLOWED_TOOLS:
            msg = "Tool execution outside frozen experiment allowlist was blocked"
            raise RuntimeError(msg)
        return handler(request)
