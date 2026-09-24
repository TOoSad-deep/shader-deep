"""带调用预算的 Deep Agent 循环, 按角色构造上下文并限制实际可执行工具."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelResponse, SummarizationMiddleware
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.exceptions import ModelAPIError
from langchain_core.messages.tool import invalid_tool_call, tool_call
from pydantic import TypeAdapter, ValidationError

from shader_deep.infrastructure.llm.transport import RAW_TOOL_CALLS, call_with_recovery
from shader_deep.runtime.events import NoProgressGuard, failure_signature
from shader_deep.runtime.execution import AnalysisLimitError, ToolFeedback
from shader_deep.runtime.submissions.parsing import decode_object, recover_object, syntax_feedback
from shader_deep.runtime.usage import request_sizes

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware import ModelRequest
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage, BaseMessage
    from langchain_core.runnables import RunnableConfig
    from pydantic import JsonValue

    from shader_deep.runtime.events import EventCallback
    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.runtime.submissions.handler import SubmissionHandler


MAX_FORMAT_REPAIR_CALLS = 2
FORMAT_ERRORS = {"json_syntax", "schema_validation"}


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
        submission_handler: SubmissionHandler | None = None,
        on_prepared: Callable[[list[BaseMessage]], None] | None = None,
        on_event: EventCallback | None = None,
        max_repeated_no_progress: int = 0,
        progress: Callable[[], object] | None = None,
        role_prompt_only: bool = False,
        before_request: Callable[[ModelRequest], None] | None = None,
        available_tools: Callable[[], list[BaseTool]] | None = None,
        on_history: Callable[[list[BaseMessage]], None] | None = None,
        repair_scope: Callable[[], str] | None = None,
        repair_scope_active: Callable[[str], bool] | None = None,
        prepare_history: Callable[[list[BaseMessage]], list[BaseMessage]] | None = None,
        on_tool_result: Callable[[dict[str, JsonValue]], None] | None = None,
    ) -> None:
        """绑定当前角色的执行状态、上下文构造入口与工具列表.

        Args:
            execution: 由程序维护的计数和执行状态.
            limit: 应用层逻辑调用上限; 0 取消调用上限, 未提供 repair_scope 时也取消格式修复上限.
            context: 构造当前任务消息, 不把临时材料持久追加到历史.
            done: 检查是否已提交通过校验的结果.
            tools: 模型可见且实际允许执行的工具列表.
            request_retries: 每次逻辑调用允许的额外暂时性错误重试次数.
            submission_handler: 完整提交与局部修复共用的草稿处理器.
            on_prepared: 请求材料组装后记录实际呈现的读取结果.
            on_event: 保存当前任务的简短执行事件.
            max_repeated_no_progress: 相同失败且无有效业务进展的连续阈值; 0 关闭.
            progress: 提供当前任务的有效业务进展, 不使用草稿版本作为进展.
            role_prompt_only: 只发送当前业务角色指令, 不附带未开放的框架文件/委派说明.
            before_request: 检查完整请求预算, 在实际呈现登记和发送前执行.
            available_tools: 按剩余能力同步控制模型可见工具与执行白名单.
            on_history: 在工具执行前统计当前响应, 并在返回时统计工具回执.
            repair_scope: 返回稳定阶段或工作身份; 提供时独立限制各身份的格式修复额度.
            repair_scope_active: 判断失败身份是否仍属于当前待修复阶段; 跳过旧阶段反馈但保留已用额度.
            prepare_history: 在构造当前上下文、检查预算和登记呈现前整理请求历史.
            on_tool_result: 保存完整工具参数与回执的制品回调, 不向模型注入记录.
        """
        self.execution, self.limit = execution, limit
        self.context, self.done, self.tools = context, done, tools
        self.request_retries = request_retries
        self.submission_handler, self.on_prepared = submission_handler, on_prepared
        self.on_event, self.progress = on_event, progress
        self.no_progress = NoProgressGuard(max_repeated_no_progress)
        self.failure_signature: str | None = None
        self.role_prompt_only = role_prompt_only
        self.role_prompt: str | None = None
        self.before_request = before_request
        self.available_tools = available_tools
        self.on_history = on_history
        self.repair_scope = repair_scope
        self.repair_scope_active = repair_scope_active
        self.prepare_history = prepare_history
        self.on_tool_result = on_tool_result

    def _active_tools(self) -> list[BaseTool]:
        return self.available_tools() if self.available_tools is not None else list(self.tools)

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
            if item.model_call == self.execution.model_calls and item.message.startswith("JSON syntax error:")
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
        if self.role_prompt_only and self.role_prompt is not None:
            return request.override(messages=messages, tools=list(self._active_tools()), system_message=SystemMessage(content=self.role_prompt))
        return request.override(messages=messages, tools=list(self._active_tools()))

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
        if self.limit and self.execution.model_calls >= self.limit:
            msg = "Analysis model-call budget exhausted"
            raise AnalysisLimitError(msg)
        repair = self._pending_repair()
        if self.prepare_history is not None:
            request = request.override(messages=cast("list[AnyMessage]", self.prepare_history(list(request.messages))))
        context = self.context()
        # 逻辑调用在此计数; 同一调用中的网络重试另存 request_attempts.
        # 恢复函数只包住模型响应获取与完整性校验, 后续工具执行不在重试范围内.
        prepared = self._with_context(request, context)
        self._check_request(prepared)
        self.no_progress.check(self.failure_signature, self.progress() if self.progress else None)
        self.failure_signature = None
        self.execution.model_calls += 1
        if self.on_event is not None:
            self.on_event("model_started", {})
            self.on_event("request_sizes", dict(request_sizes(prepared, self._active_tools())))
        response = call_with_recovery(
            lambda: _complete_response(handler(prepared)),
            self.execution,
            self.request_retries,
            on_event=self.on_event,
            before_attempt=self._attempt_guard(prepared, repair),
        )
        if self.on_event is not None:
            self.on_event("model_completed", {})
            self._record_usage(response)
        response = replace(
            response, result=[self._recover_calls(message) if isinstance(message, AIMessage) else message for message in response.result]
        )
        self._record_new_history(list(response.result))
        for message in response.result:
            if isinstance(message, AIMessage) and message.response_metadata.get("finish_reason") == "length":
                self.failure_signature = failure_signature("output_budget", None, "output_limit")
                self.execution.tool_feedback += (
                    ToolFeedback(
                        model_call=self.execution.model_calls,
                        tool="output_budget",
                        category="output_limit",
                        message="Output limit reached; no tool executed. Retry a shorter report within the logical-call budget.",
                    ),
                )
        return response

    def _attempt_guard(self, prepared: ModelRequest, repair: str | None) -> Callable[[], None]:
        """同一逻辑调用的网络尝试共用一次修复扣费, 每次仍先检查发送预算."""
        charged = False
        presented = False

        def before_attempt() -> None:
            nonlocal charged, presented
            self._check_request(prepared)
            if not presented and self.on_prepared is not None:
                self.on_prepared(list(prepared.messages))
                presented = True
            if repair is not None and not charged:
                self.execution.format_repair_calls += 1
                if self.repair_scope is not None:
                    self.execution.format_repair_scopes[repair] = self.execution.format_repair_scopes.get(repair, 0) + 1
                charged = True

        return before_attempt

    def _pending_repair(self) -> str | None:
        """按失败时的身份检查额度, 实际发送前的其他检查不消耗修复次数."""
        default_scope = self.repair_scope() if self.repair_scope is not None else "execution"
        errors = [
            item
            for index, item in enumerate(self.execution.tool_feedback)
            if item.model_call == self.execution.model_calls
            and item.category in FORMAT_ERRORS
            and index >= self.execution.format_repair_resolved_through.get(item.repair_scope or default_scope, 0)
            and (self.repair_scope_active is None or self.repair_scope_active(item.repair_scope or default_scope))
        ]
        if not errors:
            return None
        scope = errors[0].repair_scope or default_scope
        used = self.execution.format_repair_scopes.get(scope, 0) if self.repair_scope is not None else self.execution.format_repair_calls
        if (self.limit or self.repair_scope is not None) and used >= MAX_FORMAT_REPAIR_CALLS:
            msg = "Analysis format-repair budget exhausted; no valid report was fabricated"
            raise AnalysisLimitError(msg)
        return scope

    def _record_new_history(self, messages: list[BaseMessage]) -> None:
        """将本轮新增响应交给调用方计入选材预算."""
        if self.on_history is not None:
            self.on_history(messages)

    def _check_request(self, prepared: ModelRequest) -> None:
        """预算检查在呈现登记前运行, 不把未发送材料计入已读状态."""
        if self.before_request is not None:
            self.before_request(prepared)

    def _record_usage(self, response: ModelResponse[object]) -> None:
        if self.on_event is not None:
            for message in response.result:
                if isinstance(message, AIMessage) and message.usage_metadata is not None:
                    self.on_event("model_usage", cast("dict[str, JsonValue]", dict(message.usage_metadata)))

    def _recover_calls(self, message: AIMessage) -> AIMessage:
        """严格检查原始参数, 即使底层已经把残缺 JSON 解析为有效调用也不能绕过."""
        if message.response_metadata.get("finish_reason") not in {"stop", "tool_calls"}:
            return message
        allowed = {tool.name: tool for tool in self.tools}
        raw_calls = message.additional_kwargs.get(RAW_TOOL_CALLS, [])
        if not raw_calls and (message.tool_calls or message.invalid_tool_calls):
            # 缺少原文时拒绝执行, 避免对未审查的客户端解析结果作乐观假设.
            raw_calls = [{"name": call.get("name"), "id": call.get("id"), "args": ""} for call in message.tool_calls]
            raw_calls += [{"name": call.get("name"), "id": call.get("id"), "args": call.get("args")} for call in message.invalid_tool_calls]
        accepted, remaining = [], []
        for raw_call in raw_calls:
            name, identifier, raw = raw_call.get("name") or "unknown", raw_call.get("id"), raw_call.get("args")
            raw = raw if isinstance(raw, str) else ""
            try:
                value = decode_object(raw)
            except (TypeError, ValueError, RecursionError):
                selected = allowed.get(name)
                value = recover_object(raw, selected) if selected is not None and identifier else None
                if value is None:
                    remaining.append(invalid_tool_call(name=name, args=raw, id=identifier, error=syntax_feedback(raw)))
                    continue
                self.execution.tool_feedback += (
                    ToolFeedback(
                        model_call=self.execution.model_calls,
                        tool=name,
                        category="syntax_recovered",
                        message=(
                            "Recovered unchanged schema-valid JSON object by discarding surplus closing delimiters; normal tool checks still apply."
                        ),
                    ),
                )
            if not identifier:
                remaining.append(invalid_tool_call(name=name, args=raw, id=identifier, error="Missing tool call ID"))
            else:
                accepted.append(tool_call(name=name, args=value, id=identifier))
        for invalid in remaining:
            self.failure_signature = failure_signature(invalid.get("name") or "unknown", invalid.get("args"), invalid.get("error"))
            self._record_invalid_call(invalid.get("name") or "unknown", invalid.get("id"), invalid.get("args"), invalid.get("error"))
            if self.on_event is not None:
                self.on_event("tool_rejected", {"tool": invalid.get("name") or "unknown", "category": "json_syntax"})
            self.execution.tool_feedback += (
                ToolFeedback(
                    model_call=self.execution.model_calls,
                    tool=invalid.get("name") or "unknown",
                    category="json_syntax",
                    message=invalid.get("error") or syntax_feedback(invalid.get("args") or ""),
                    repair_scope=self.repair_scope() if self.repair_scope is not None else None,
                ),
            )
        # 未执行的非法调用不能留在历史中: 适配器会将其重新编码为 tool_calls,
        # 但执行器没有为它生成 ToolMessage, 导致下一次请求出现未配对调用.
        # 保留短诊断供下一轮上下文读取, 空响应转为普通文本以满足服务端消息约束.
        content = message.content or ("工具参数无效, 未执行被拒绝的调用。" if remaining and not accepted else "")
        extra = {key: value for key, value in message.additional_kwargs.items() if key not in {"tool_calls", RAW_TOOL_CALLS}}
        return message.model_copy(update={"content": content, "tool_calls": accepted, "invalid_tool_calls": [], "additional_kwargs": extra})

    def _record_invalid_call(self, name: str, identity: str | None, arguments: str | None, error: str | None) -> None:
        """未执行的非法 JSON 也留存原始参数, 便于对照语法拒绝与业务拒绝."""
        details: dict[str, JsonValue] = {
            "tool": name,
            "call_id": identity,
            "outcome": "rejected",
            "category": "json_syntax",
            "scope": self.repair_scope() if self.repair_scope is not None else None,
        }
        if self.on_event is not None:
            self.on_event("tool_result", details)
        if self.on_tool_result is not None:
            self.on_tool_result({**details, "arguments": arguments, "response": error})

    def _tool_error(self, request: ToolCallRequest, message: str, category: str, scope: str | None) -> ToolMessage:
        """保存有长度上限的诊断, 并用原调用 ID 返回配对的错误消息."""
        self.execution.tool_feedback += (
            ToolFeedback(
                model_call=self.execution.model_calls,
                tool=request.tool_call["name"],
                message=message[:1400],
                category=category,
                repair_scope=scope,
            ),
        )
        self.failure_signature = failure_signature(request.tool_call["name"], request.tool_call["args"], message)
        if self.on_event is not None:
            self.on_event("tool_rejected", {"tool": request.tool_call["name"], "category": category, "message": message[:800]})
        return ToolMessage(content=message[:1400], tool_call_id=request.tool_call["id"], status="error")

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], object]) -> ToolMessage:
        """拒绝当前分析角色未开放的内置委派与文件系统工具.

        Args:
            request: 模型提出的工具调用.
            handler: 框架提供的工具执行入口.

        Returns:
            工具执行结果或明确的拒绝信息.
        """
        scope = self.repair_scope() if self.repair_scope is not None else None
        result = self._execute_tool(request, handler, scope)
        self._record_tool_result(request, result, scope)
        self._record_new_history([result])
        return result

    def _record_tool_result(self, request: ToolCallRequest, result: ToolMessage, scope: str | None) -> None:
        """完整交互交给制品回调, 事件仅保留身份和结果类别."""
        try:
            payload = json.loads(result.content) if isinstance(result.content, str) else {}
        except (ValueError, TypeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        rejected = result.status == "error" or payload.get("status") in {"error", "rejected", "invalid_submission", "not_selected"}
        omitted = payload.get("not_provided_ids")
        outcome = "rejected" if rejected else "partial" if omitted else "success"
        details: dict[str, JsonValue] = {
            "tool": request.tool_call["name"],
            "call_id": request.tool_call["id"],
            "outcome": outcome,
            "scope": scope,
        }
        if self.on_event is not None:
            self.on_event("tool_result", details)
        if self.on_tool_result is not None:
            self.on_tool_result({**details, "arguments": request.tool_call["args"], "response": result.content})

    def _execute_tool(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], object], scope: str | None) -> ToolMessage:
        """执行一次工具, 由外层统一统计成功或拒绝回执的历史大小."""
        if request.tool_call["name"] not in {tool.name for tool in self._active_tools()}:
            return self._tool_error(request, "当前分析角色未开放此工具。", "permission", scope)
        selected = next(tool for tool in self.tools if tool.name == request.tool_call["name"])
        if self.submission_handler is not None:
            reply = self.submission_handler.handle(request.tool_call["name"], request.tool_call["args"])
            if reply is not None:
                current = reply.saved
                if self.on_event is not None and current is not None:
                    self.on_event(
                        "submission_saved",
                        {
                            "draft_id": str(current["draft_id"]),
                            "revision": cast("int", current["revision"]),
                            "invoked_tool": request.tool_call["name"],
                            "submission_tool": str(current["tool_name"]),
                            "accepted": reply.category is None,
                        },
                    )
                if reply.category is not None:
                    self._tool_error(request, reply.content, reply.category, scope)
                    self.failure_signature = reply.signature
                else:
                    self.execution.format_repair_resolved_through[scope or "execution"] = len(self.execution.tool_feedback)
                return ToolMessage(content=reply.content, tool_call_id=request.tool_call["id"], status="error" if reply.category else "success")
        # 先做结构校验, 工具内部再检查业务引用; 两者失败都反馈给模型按预算修正.
        # include_input=False 避免把整个原始报告再次写入错误反馈.
        try:
            TypeAdapter(selected.tool_call_schema).validate_python(request.tool_call["args"])
        except ValidationError as exc:
            errors = [f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors(include_input=False)[:4]]
            message = "参数校验失败, 请修正后完整提交: \n" + "\n".join(errors)
            if any("report.summary" in item for item in errors):
                message += "\nsummary 是提交工具的顶层参数, 与 report 并列, 不属于 report。"
            return self._tool_error(request, message, "schema_validation", scope)
        feedback_count = len(self.execution.tool_feedback)
        try:
            result = cast("ToolMessage", handler(request))
        except (KeyError, TypeError, ValueError) as exc:
            return self._tool_error(request, f"提交被拒绝: {str(exc)[:800]}", "business_rejection", scope)
        if result.status == "error":
            return self._tool_error(request, str(result.content), "business_rejection", scope)
        self._record_business_feedback(request, feedback_count)
        return result

    def _record_business_feedback(self, request: ToolCallRequest, feedback_count: int) -> None:
        """会话内已保存的普通回执拒绝, 同样参与事件与停滞识别."""
        # 批次和测量工具沿用结构化普通回执, 业务拒绝由会话先登记到执行记录.
        # 不改写这些回执, 但其失败同样参与下一轮的停滞识别.
        for feedback in self.execution.tool_feedback[feedback_count:]:
            if feedback.category == "business_rejection":
                self.failure_signature = failure_signature(request.tool_call["name"], request.tool_call["args"], feedback.message)
                if self.on_event is not None:
                    self.on_event("tool_rejected", {"tool": feedback.tool, "category": feedback.category, "message": feedback.message[:800]})

    def run(self, model: BaseChatModel, prompt: str, config: RunnableConfig) -> None:
        """运行至有效提交、失败或当前角色的调用预算耗尽.

        Args:
            model: 已配置好的模型客户端.
            prompt: 当前角色的系统指令.
            config: 追踪元数据与框架执行配置.
        """
        self.role_prompt = prompt
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
        if self.on_event is not None:
            self.on_event("task_started", {})
        # agent.invoke 自身已有模型与工具循环; 外层处理只回复文字、未有效提交的情况.
        # 继续沿用返回的有效历史, 调用预算由本实例跨多次 invoke 累计.
        # 同一响应的工具按声明顺序执行, 防止停止与提交争抢会话锁.
        # 仅限制本角色图; 工作流的独立探索批次仍使用自己的线程池.
        invocation_config: RunnableConfig = {**config, "max_concurrency": 1}
        while not self.done():
            result = agent.invoke({"messages": messages}, config=invocation_config)
            last = result["messages"][-1]
            prompt = "尚未提交有效结果, 请按工具反馈修正或继续分析。"
            if isinstance(last, AIMessage) and last.response_metadata.get("finish_reason") == "length":
                prompt = "上次响应达到输出预算且未执行工具。直接提交更短的结构化结果, 仅保留必要的业务条目, 未解决内容保留为具体未知项。"
            messages = [*result["messages"], HumanMessage(content=prompt)]
        self.execution.status = "completed"
        if self.on_event is not None:
            self.on_event("task_completed", {})


def _complete_response(response: ModelResponse[object]) -> ModelResponse[object]:
    """在交给工具执行器前, 拒绝截断输出或缺少正常结束标记的工具参数."""
    # 流式解析器可能在本地补齐截断的 JSON, 不能据此认定响应已完整结束.
    # 允许执行解析出的工具调用前, 必须检查服务端的正常结束标记.
    for message in response.result:
        if isinstance(message, AIMessage) and message.response_metadata.get("finish_reason") == "length":
            return ModelResponse(result=[AIMessage(content="输出达到单次预算, 未执行工具。", response_metadata={"finish_reason": "length"})])
        if (
            isinstance(message, AIMessage)
            and (message.tool_calls or message.invalid_tool_calls or message.additional_kwargs.get(RAW_TOOL_CALLS))
            and message.response_metadata.get("finish_reason") not in {"stop", "tool_calls"}
        ):
            msg = "Incomplete model response: no successful finish marker for tool arguments"
            raise ModelAPIError(msg)
    return response
