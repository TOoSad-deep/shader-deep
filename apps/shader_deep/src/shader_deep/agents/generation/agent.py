"""生成角色的模型循环与工具装配."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from langchain.messages import HumanMessage

from shader_deep.agents.generation.middleware import GenerationContextMiddleware, GenerationLoopMiddleware
from shader_deep.agents.generation.prompts import SYSTEM_PROMPT
from shader_deep.infrastructure.llm.client import build_model

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from shader_deep.agents.generation.tools import RenderSession


def _run_config(session: RenderSession) -> RunnableConfig:
    # 用同一 session_id 关联多次 agent.invoke 的追踪记录与本地制品.
    # recursion_limit 是框架图步数上限, 不等于模型轮数或渲染次数预算.
    return {
        "run_name": "shader-deep.generation",
        "tags": ["shader-deep", "generation"],
        "metadata": {
            "session_id": session.run_dir.name,
            "task_id": session.task_id,
            "target_version": session.state["tasks"][session.task_id].target_version,
            "run_dir": str(session.run_dir),
            "width": session.options.width,
            "height": session.options.height,
            "time": session.options.time,
            "max_attempts": session.options.max_attempts,
        },
        "recursion_limit": 4 * (session.options.max_attempts + 2) + 10,
    }


def _execute(session: RenderSession) -> None:
    # tools() 将绑定会话的方法包装成模型 Tool; 模型只需提交业务参数.
    tools = session.tools()
    agent = create_deep_agent(
        model=build_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[
            # 先加载最新材料, 再登记哪些成功预览将随本轮请求提供给模型.
            # lambda 每次取 session.state, 避免捕获后续已经过时的黑板快照.
            GenerationContextMiddleware(lambda: session.state, session.task_id, asset_root=session.asset_root),
            GenerationLoopMiddleware(session, tools),
        ],
    )
    messages = [HumanMessage(content="完成本次生成任务: 渲染候选, 查看预览, 最后使用 finish_shader 选择已检查的版本.")]
    # invoke 内部已包含模型与工具之间的循环. 外层循环处理模型只回复文字的情况:
    # 一次图执行结束仍未选择候选, 就携带返回的历史继续, 总预算由 session 跨调用累计.
    while session.selected is None:
        result = agent.invoke({"messages": messages}, config=_run_config(session))
        messages = [*result["messages"], HumanMessage(content="本轮尚未结束. 请调用 render_shader, 或查看已有预览后调用 finish_shader.")]
