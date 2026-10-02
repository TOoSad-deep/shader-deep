"""主 Agent 按真实读取的编排 skill 持续选择受限业务工具."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.tools import tool
from langchain_openai import ChatOpenAI

from shader_deep.agents.five_analysis.agent import _active, _charge_model, _control_context
from shader_deep.agents.five_analysis.audit import RoleAudit
from shader_deep.agents.main.loop import MainAgentLoop
from shader_deep.agents.main.skill import OrchestrationSkill
from shader_deep.domain.five_libraries import Element  # noqa: TC001  # 工具装饰器运行时解析元素类型.
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.budgets import check_request
from shader_deep.workflows.configuration import resolve_phase_options

if TYPE_CHECKING:
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel

    from shader_deep.agents.main.contracts import MainAgentActions
    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.runtime.task_store import Attempt, TaskStore
    from shader_deep.workflows.options import AnalysisOptions


MAIN_PROMPT = """你是持续主 Agent, 拥有本轮独立上下文和执行循环。
根据可读取的编排 skill 和真实工具回执组织分析, 用本轮开放的业务工具执行动作。
当前权限只包含请求中列出的工具; 任务、状态、校验及交付由工具实现负责。
元素登记等中间动作成功后继续执行, 直到 finish_analysis 接受最终结果。
"""


def _business_tools(actions: MainAgentActions) -> list[BaseTool]:
    """只包装宿主提供的业务能力, 不自行解释任务状态."""

    @tool
    def submit_elements(elements: tuple[Element, ...]) -> str:
        """登记具体元素及范围, 成功后继续主会话.

        Args:
            elements: 原图元素目录, feature_ids 为空.
        """
        return json.dumps(actions.submit_elements(elements), ensure_ascii=False)

    @tool
    def dispatch_exploration(target_element_id: str, directions: tuple[str, ...]) -> str:
        """根据已收到的登记回执选择目标, 执行本轮独立探索并返回最终状态.

        Args:
            target_element_id: 已登记的唯一目标 ID.
            directions: 本轮固定数量的独立探索方向.
        """
        return json.dumps(actions.dispatch_exploration(target_element_id, directions), ensure_ascii=False)

    @tool
    def read_analysis_result(result_id: str) -> str:
        """读取本轮已接受结果的完整正文.

        Args:
            result_id: 工具回执提供的结果引用.
        """
        return json.dumps(actions.read_analysis_result(result_id), ensure_ascii=False)

    @tool
    def dispatch_integration() -> str:
        """请求程序汇集并保存 V0, 再执行独立整合并返回版本引用."""
        return json.dumps(actions.dispatch_integration(), ensure_ascii=False)

    @tool
    def finish_analysis() -> str:
        """请求程序核对结果、计算最终状态并结束主会话."""
        return json.dumps(actions.finish_analysis(), ensure_ascii=False)

    return [submit_elements, dispatch_exploration, read_analysis_result, dispatch_integration, finish_analysis]


def run_main_agent(
    store: TaskStore,
    attempt: Attempt,
    model: BaseChatModel,
    options: AnalysisOptions,
    execution: AnalysisExecution,
    request: str,
    reference_url: str,
    actions: MainAgentActions,
) -> None:
    """使用同一 Deep Agent 连续读取 skill、登记、派发、整合及交付.

    Args:
        store: 权威持久任务记录.
        attempt: 本次 main 执行身份.
        model: 既有模型客户端.
        options: 分析预算与配置.
        execution: 本次逻辑调用与反馈记录.
        request: 用户要求.
        reference_url: 固定原图的多模态引用.
        actions: 宿主提供的受限业务执行接口.
    """
    skill = OrchestrationSkill()
    tools = [skill.tool(), *_business_tools(actions)]
    phase_options, _ = resolve_phase_options(options, "outline")
    configured = configure_analysis_model(model, phase_options) if isinstance(model, ChatOpenAI) else model
    audit = RoleAudit(store, attempt, execution)
    loop = MainAgentLoop(
        execution,
        options.max_main_calls,
        lambda: _control_context(
            _message({"user_request": request, "default_directions": min(options.max_tasks, 3), **actions.context()}, reference_url), store, attempt
        ),
        actions.done,
        tools,
        request_retries=min(options.max_request_retries, 2),
        role_prompt_only=True,
        before_request=lambda prepared: check_request(prepared, loop._active_tools(), phase_options),
        repair_scope=lambda: attempt.task_id,
        progress=lambda: (skill.presented, actions.progress()),
        on_prepared=skill.mark_presented,
        on_event=audit.event,
        on_tool_result=audit.tool,
        on_history=audit.history,
    )
    loop.actions, loop.skill = actions, skill
    loop.repair_charge = lambda: store.consume_repair(attempt)
    loop.model_charge = lambda: _charge_model(store, attempt, options, "outline")
    loop.inactive_check = lambda: _active(store, attempt)
    loop.run(
        configured,
        MAIN_PROMPT,
        {"run_name": "main", "metadata": {"task_id": attempt.task_id, "attempt_id": attempt.attempt_id}, "recursion_limit": 1000},
    )
