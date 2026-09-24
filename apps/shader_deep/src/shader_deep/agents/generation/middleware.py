"""在每次模型调用前注入生成任务材料, 保留框架提供的有效工作历史."""

# Context Builder 负责准备材料, middleware 负责决定何时把材料放进模型请求.
# 当前执行入口使用同步 agent.invoke; 两个 middleware 的注册顺序见 agents/generation.py.

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelResponse
from langchain.messages import AIMessage, HumanMessage, ToolMessage

from shader_deep.agents.generation.context import build_generation_context
from shader_deep.agents.generation.tools import GenerationLimitError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain.tools import BaseTool

    from shader_deep.agents.generation.tools import RenderSession
    from shader_deep.domain.tasks import BlackboardState


class GenerationContextMiddleware(AgentMiddleware[AgentState[object], None, object]):
    """为一个固定生成任务逐轮读取黑板, 并构造本次请求的材料."""

    def __init__(self, get_state: Callable[[], BlackboardState], task_id: str, *, asset_root: Path | None = None) -> None:
        """绑定任务和黑板读取入口.

        Args:
            get_state: 返回调用方当前黑板状态的函数, 支持任务内登记新结果后刷新材料.
            task_id: 本实例负责的生成任务.
            asset_root: 相对制品路径的根目录, 未提供时固定使用构造实例时的工作目录.
        """
        self.get_state = get_state
        self.task_id = task_id
        self.asset_root = (asset_root if asset_root is not None else Path.cwd()).resolve()

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        # 每次调用 getter 取最新黑板, 包含上一轮工具刚登记的候选与错误.
        context = build_generation_context(self.get_state(), self.task_id, asset_root=self.asset_root)
        # override 仅修改本次请求, 材料消息不写入会话历史, 避免每轮重复累积.
        return request.override(messages=[context.message, *request.messages])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse[object]]) -> ModelResponse[object]:
        """同步调用前加入任务材料.

        Args:
            request: 包含框架有效历史和工具定义的请求.
            handler: 后续中间件和模型的调用入口.

        Returns:
            后续处理链返回的模型结果.
        """
        # handler 是后续中间件/模型调用链; 先准备请求, 再交给它执行.
        return handler(self._prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse[object]]]
    ) -> ModelResponse[object]:
        """异步调用前加入同一套任务材料.

        Args:
            request: 包含框架有效历史和工具定义的请求.
            handler: 后续中间件和模型的异步调用入口.

        Returns:
            后续处理链返回的模型结果.
        """
        return await handler(self._prepare(request))


class GenerationLoopMiddleware(AgentMiddleware[AgentState[object], None, object]):
    """在任务材料已构造后检查预算, 并仅允许本轮渲染与结束工具."""

    def __init__(self, session: RenderSession, tools: list[BaseTool]) -> None:
        """绑定运行会话和允许的工具.

        Args:
            session: 维护候选、计数和选择状态的会话.
            tools: 本次 Agent 注册的渲染与结束工具.
        """
        self.session = session
        self.allowed_tools = tools

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        session = self.session
        # 模型轮数与渲染次数分开限制: 模型可能只说话、提交无效工具参数或反复选择失败.
        # 多留两轮供纠错/选择, 但实际完成也可能在预算内提前发生.
        if session.model_calls >= session.options.max_attempts + 2:
            session.stop_reason = "attempt_limit" if session.attempts >= session.options.max_attempts else "model_limit"
            session.save()
            raise GenerationLimitError(session.stop_reason)
        # 前一个 Context middleware 已实际加载本轮成功候选的 PNG.
        # presented 记录预览进入本轮请求的资格, 不证明模型完成了有效视觉判断.
        # 同一批工具调用中的新渲染尚未经过下一次模型调用, 不能立即选择.
        session.presented.update(session.successful)
        # 计数在调用前递增; 不包含模型客户端内部的 HTTP 重试次数.
        session.model_calls += 1
        session.save()
        conditions = HumanMessage(
            content=json.dumps(
                {
                    "render_conditions": {"width": session.options.width, "height": session.options.height, "time": session.options.time},
                    "attempts_remaining": session.options.max_attempts - session.attempts,
                    "model_calls_remaining": session.options.max_attempts + 2 - session.model_calls,
                    "finishable_candidates": sorted(session.presented),
                },
                ensure_ascii=False,
            )
        )
        # 向模型公开的工具定义只保留本轮两个工具; 执行时还会再检查一次名字.
        return request.override(messages=[*request.messages, conditions], tools=list(self.allowed_tools))

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse[object]]) -> ModelResponse[object]:
        """选定候选后直接交付其代码, 否则在预算内调用模型.

        Args:
            request: 已含任务材料和工具反馈的请求.
            handler: 后续模型调用入口.

        Returns:
            模型响应, 或已选定且实际渲染过的代码.
        """
        if self.session.selected is not None:
            # finish 工具之后, 图可能再次走到模型节点; 直接返回已渲染源码, 节省一次请求.
            code = Path(self.session.selected.code_path).read_text(encoding="utf-8")
            return ModelResponse(result=[AIMessage(content=code)])
        return handler(self._prepare(request))

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], object]) -> ToolMessage:
        """拒绝模型调用本轮未开放的内置工具.

        Args:
            request: 模型提出的工具调用.
            handler: 执行工具并生成 ToolMessage 的入口.

        Returns:
            本轮工具的文本反馈或不执行该工具的错误说明.
        """
        if request.tool_call["name"] not in {tool.name for tool in self.allowed_tools}:
            # 即使模型猜到框架内置工具名, 也在执行前返回错误, 不调用对应 handler.
            return ToolMessage(content="本轮只允许 render_shader 和 finish_shader", tool_call_id=request.tool_call["id"], status="error")
        # 这两个工具只返回字符串, 因此本流程的 handler 返回 ToolMessage.
        return cast("ToolMessage", handler(request))
