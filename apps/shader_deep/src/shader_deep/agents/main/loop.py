"""持续主 Agent 的请求权限快照与 skill 读取门禁."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware, AgentState, SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from shader_deep.agents.five_analysis.loop import FiveAnalysisLoop
from shader_deep.runtime.execution import AnalysisLimitError

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.agents.middleware import ModelRequest
    from langchain.agents.middleware.types import ToolCallRequest
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import ToolMessage
    from langchain_core.runnables import RunnableConfig

    from shader_deep.agents.main.contracts import MainAgentActions
    from shader_deep.agents.main.skill import OrchestrationSkill


class MainAgentLoop(FiveAnalysisLoop):
    """执行时同时检查请求权限和最新业务权限, 同轮状态变化不能扩权."""

    actions: MainAgentActions
    skill: OrchestrationSkill
    request_tools: frozenset[str] = frozenset()

    def _active_tools(self) -> list[BaseTool]:
        allowed = set(self.actions.allowed_tools()) if self.skill.in_request else {"read_skill"}
        return [item for item in self.tools if item.name in allowed]

    def _with_context(self, request: ModelRequest, context: HumanMessage) -> ModelRequest:
        """清理框架通用权限说明后重新注入 SDK 实际发现的 skill 信息."""
        metadata = request.state.get("skills_metadata", [])
        if not any(item.get("name") == "analysis-orchestration" for item in metadata):
            msg = "SDK 未发现可用的 analysis-orchestration skill"
            raise AnalysisLimitError(msg)
        self.skill.in_request = self.skill.presented or self.skill.full_response(list(request.messages))
        prepared = self.skill.middleware.modify_request(super()._with_context(request, context))
        self.request_tools = frozenset(tool.name for tool in self._active_tools())
        return prepared

    def _execute_tool(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], object], scope: str | None) -> ToolMessage:
        """拒绝模型夹带本轮请求未开放的动作, 即使前一个调用刚完成登记."""
        if request.tool_call["name"] not in self.request_tools:
            return self._tool_error(request, "本次模型请求尚未开放此工具, 请根据下一轮回执继续。", "permission", scope)
        return super()._execute_tool(request, handler, scope)

    def run(self, model: BaseChatModel, prompt: str, config: RunnableConfig) -> None:
        """持续调用直到最终提交, 保留独立模型历史与串行工具执行.

        Args:
            model: 已配置传输保护的模型.
            prompt: 稳定角色和权限说明.
            config: 追踪身份及框架执行配置.
        """
        self.role_prompt = prompt
        agent = create_deep_agent(
            model=model,
            tools=self.tools,
            system_prompt=prompt,
            backend=self.skill.backend,
            skills=["/skills/"],
            middleware=[
                cast("AgentMiddleware[AgentState[object], None, object]", self.skill.middleware),
                # 业务结果由 TaskStore 保存并完整回读; 禁用 SDK 自动写入安装资源及预览替换.
                cast(
                    "AgentMiddleware[AgentState[object], None, object]",
                    FilesystemMiddleware(
                        backend=self.skill.backend,
                        tool_token_limit_before_evict=None,
                        human_message_token_limit_before_evict=None,
                    ),
                ),
                SummarizationMiddleware(model=model, trigger=None),
                self,
            ],
        )
        messages = [HumanMessage(content="读取编排 skill, 按实际回执完成本轮分析。")]
        self.execution.status = "running"
        if self.on_event is not None:
            self.on_event("task_started", {})
        invocation_config: RunnableConfig = {**config, "max_concurrency": 1}
        while not self.done():
            result = agent.invoke({"messages": messages}, config=invocation_config)
            last = result["messages"][-1]
            reminder = "本轮尚未交付, 按编排 skill 和工具反馈继续选择下一项业务动作。"
            if isinstance(last, AIMessage) and last.response_metadata.get("finish_reason") == "length":
                reminder = "上次输出达到预算且工具未执行, 缩短参数后继续当前业务动作。"
            messages = [*result["messages"], HumanMessage(content=reminder)]
        self.execution.status = "completed"
        if self.on_event is not None:
            self.on_event("task_completed", {})
