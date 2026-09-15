"""带调用预算的 Deep Agent 循环, 按角色构造上下文并限制实际可执行工具."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelResponse, SummarizationMiddleware
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.exceptions import ModelAPIError
from langchain_core.messages.tool import tool_call
from pydantic import TypeAdapter, ValidationError

from shader_deep.analysis.tool_json import recover_object, syntax_feedback
from shader_deep.analysis.transport import call_with_recovery
from shader_deep.analysis.types import AnalysisLimitError, ToolFeedback

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware import ModelRequest
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig

    from shader_deep.analysis.types import AnalysisExecution


class AnalysisLoop(AgentMiddleware[AgentState[object], None, object]):
    """每次角色执行使用独立实例, 确保子任务的消息历史相互隔离."""

    def __init__(
        self,
        execution: AnalysisExecution,
        limit: int,
        context: Callable[[], HumanMessage],
        done: Callable[[], bool],
        tools: list[BaseTool],
        *,
        request_retries: int = 0,
    ) -> None:
        """绑定当前角色的执行状态、上下文构造入口与工具列表.

        Args:
            execution: 由程序维护的计数和执行状态.
            limit: 应用层逻辑模型调用次数上限.
            context: 构造当前任务消息, 不把临时材料持久追加到历史.
            done: 检查是否已提交通过校验的结果.
            tools: 模型可见且实际允许执行的工具列表.
            request_retries: 每次逻辑调用允许的额外暂时性错误重试次数.
        """
        self.execution, self.limit = execution, limit
        self.context, self.done, self.tools = context, done, tools
        self.request_retries = request_retries

    def _with_context(self, request: ModelRequest, context: HumanMessage) -> ModelRequest:
        """仅重建本次请求, 将新任务材料与上一轮 JSON 诊断一起交给模型."""
        # 既有接口兼容处理: 连续 user 消息可能导致当前多模态接口无法读取图片.
        # 将新材料合并到末尾用户消息, 或追加在完整的模型与工具交互之后,
        # 保持工具调用与工具响应配对, 避免破坏对话结构.
        messages = list(request.messages)
        # 只重复上一逻辑轮的语法错误, 避免旧诊断跨轮累积或干扰已修正的报告.
        diagnostics = [
            item.message
            for item in self.execution.tool_feedback
            if item.model_call == self.execution.model_calls - 1 and item.message.startswith("JSON syntax error:")
        ]
        if diagnostics:
            blocks = [{"type": "text", "text": context.content}] if isinstance(context.content, str) else list(context.content)
            blocks.append({"type": "text", "text": "\n".join(diagnostics)})
            context = context.model_copy(update={"content": blocks})
        if messages and isinstance(messages[-1], HumanMessage):
            current = messages[-1]
            current_blocks = [{"type": "text", "text": current.content}] if isinstance(current.content, str) else current.content
            context_blocks = [{"type": "text", "text": context.content}] if isinstance(context.content, str) else context.content
            messages[-1] = current.model_copy(update={"content": [*current_blocks, *context_blocks]})
        else:
            messages.append(context)
        return request.override(messages=messages, tools=list(self.tools))

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse[object]]) -> ModelResponse[object]:
        """刷新实际图像输入, 并在请求前累计逻辑模型调用次数.

        Args:
            request: 当前模型请求.
            handler: 后续中间件与模型调用链.

        Returns:
            模型响应; 已提交结果时直接返回结束消息.
        """
        if self.done():
            return ModelResponse(result=[AIMessage(content="分析结果已提交。")])
        if self.execution.model_calls >= self.limit:
            msg = "Analysis model-call budget exhausted"
            raise AnalysisLimitError(msg)
        context = self.context()
        # 逻辑调用在此计数; 同一调用中的网络重试另存 request_attempts.
        # 恢复函数只包住模型响应获取与完整性校验, 后续工具执行不在重试范围内.
        self.execution.model_calls += 1
        prepared = self._with_context(request, context)
        response = call_with_recovery(lambda: _complete_response(handler(prepared)), self.execution, self.request_retries)
        response = replace(
            response, result=[self._recover_calls(message) if isinstance(message, AIMessage) else message for message in response.result]
        )
        for message in response.result:
            if isinstance(message, AIMessage):
                if message.response_metadata.get("finish_reason") == "length":
                    self.execution.tool_feedback += (
                        ToolFeedback(
                            model_call=self.execution.model_calls,
                            tool="output_budget",
                            message="Output limit reached; no tool executed. Retry a shorter report within the logical-call budget.",
                        ),
                    )
                for invalid in message.invalid_tool_calls:
                    self.execution.tool_feedback += (
                        ToolFeedback(
                            model_call=self.execution.model_calls,
                            tool=invalid.get("name") or "unknown",
                            message=syntax_feedback(invalid.get("args") or ""),
                        ),
                    )
        return response

    def _recover_calls(self, message: AIMessage) -> AIMessage:
        """只恢复正常结束响应中的有限 JSON 尾部错误, 保留无法恢复的调用."""
        if message.response_metadata.get("finish_reason") not in {"stop", "tool_calls"}:
            return message
        allowed = {tool.name: tool for tool in self.tools}
        repaired, remaining = [], []
        for invalid in message.invalid_tool_calls:
            selected = allowed.get(invalid.get("name") or "")
            if selected is None or not invalid.get("id"):
                remaining.append(invalid)
                continue
            value = recover_object(invalid.get("args") or "", selected)
            if value is None:
                remaining.append(invalid)
                continue
            repaired.append(tool_call(name=selected.name, args=value, id=invalid["id"]))
            self.execution.tool_feedback += (
                ToolFeedback(
                    model_call=self.execution.model_calls,
                    tool=selected.name,
                    message="Recovered unchanged schema-valid JSON object by discarding surplus closing delimiters; normal tool checks still apply.",
                ),
            )
        if not repaired:
            return message
        # 清除兼容字段中的原始调用副本, 让显式更新后的 tool_calls 成为唯一解析结果.
        # 修复语法不代表允许执行; 后面仍需经过工具白名单和业务规则校验.
        extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
        return message.model_copy(
            update={"tool_calls": [*message.tool_calls, *repaired], "invalid_tool_calls": remaining, "additional_kwargs": extra}
        )

    def _tool_error(self, request: ToolCallRequest, message: str) -> ToolMessage:
        """保存有长度上限的诊断, 并用原调用 ID 返回配对的错误消息."""
        self.execution.tool_feedback += (
            ToolFeedback(
                model_call=self.execution.model_calls,
                tool=request.tool_call["name"],
                message=message[:1400],
            ),
        )
        return ToolMessage(content=message[:1400], tool_call_id=request.tool_call["id"], status="error")

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], object]) -> ToolMessage:
        """拒绝当前分析角色未开放的内置委派与文件系统工具.

        Args:
            request: 模型提出的工具调用.
            handler: 框架提供的工具执行入口.

        Returns:
            工具执行结果或明确的拒绝信息.
        """
        if request.tool_call["name"] not in {tool.name for tool in self.tools}:
            return self._tool_error(request, "当前分析角色未开放此工具。")
        selected = next(tool for tool in self.tools if tool.name == request.tool_call["name"])
        # 先做结构校验, 工具内部再检查业务引用; 两者失败都反馈给模型按预算修正.
        # include_input=False 避免把整个原始报告再次写入错误反馈.
        try:
            TypeAdapter(selected.get_input_schema()).validate_python(request.tool_call["args"])
        except ValidationError as exc:
            errors = [f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors(include_input=False)[:4]]
            message = "参数校验失败, 请修正后完整提交: \n" + "\n".join(errors)
            if any("report.summary" in item for item in errors):
                message += "\nsummary 是提交工具的顶层参数, 与 report 并列, 不属于 report。"
            return self._tool_error(request, message)
        try:
            return cast("ToolMessage", handler(request))
        except (KeyError, TypeError, ValueError) as exc:
            return self._tool_error(request, f"提交被拒绝: {str(exc)[:800]}")

    def run(self, model: BaseChatModel, prompt: str, config: RunnableConfig) -> None:
        """运行至有效提交、失败或当前角色的调用预算耗尽.

        Args:
            model: 已配置好的模型客户端.
            prompt: 当前角色的系统指令.
            config: 追踪元数据与框架执行配置.
        """
        # 此分析循环在预算内保留完整短历史, 关闭自动调用模型生成摘要的机制,
        # 避免摘要模型请求绕过当前角色的模型调用计数.
        agent = create_deep_agent(
            model=model,
            tools=self.tools,
            system_prompt=prompt,
            middleware=[SummarizationMiddleware(model=model, trigger=None), self],
        )
        messages = [HumanMessage(content="完成当前分析任务, 使用本角色的提交工具返回结果。")]
        self.execution.status = "running"
        # agent.invoke 自身已有模型与工具循环; 外层处理只回复文字、未有效提交的情况.
        # 继续沿用返回的有效历史, 调用预算由本实例跨多次 invoke 累计.
        while not self.done():
            result = agent.invoke({"messages": messages}, config=config)
            last = result["messages"][-1]
            prompt = "尚未提交有效结果, 请按工具反馈修正或继续分析。"
            if isinstance(last, AIMessage) and last.response_metadata.get("finish_reason") == "length":
                prompt = "上次响应达到输出预算且未执行工具。直接提交更短的结构化结果, 仅保留最关键的观察和解释, 其他内容保留为未知项。"
            messages = [*result["messages"], HumanMessage(content=prompt)]
        self.execution.status = "completed"


def _complete_response(response: ModelResponse[object]) -> ModelResponse[object]:
    """在交给工具执行器前, 拒绝截断输出或缺少正常结束标记的工具参数."""
    # 流式解析器可能在本地补齐截断的 JSON, 不能据此认定响应已完整结束.
    # 允许执行解析出的工具调用前, 必须检查服务端的正常结束标记.
    for message in response.result:
        if isinstance(message, AIMessage) and message.response_metadata.get("finish_reason") == "length":
            return ModelResponse(result=[AIMessage(content="输出达到单次预算, 未执行工具。", response_metadata={"finish_reason": "length"})])
        if isinstance(message, AIMessage) and message.tool_calls and message.response_metadata.get("finish_reason") not in {"stop", "tool_calls"}:
            msg = "Incomplete model response: no successful finish marker for tool arguments"
            raise ModelAPIError(msg)
    return response
