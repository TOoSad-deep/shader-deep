"""生成角色的模型循环与工具装配."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.messages import HumanMessage
from langchain_core.callbacks import UsageMetadataCallbackHandler

from shader_deep.agents.generation.middleware import GenerationContextMiddleware, GenerationLoopMiddleware
from shader_deep.agents.generation.prompts import MEMORY_PROMPT, SYSTEM_PROMPT
from shader_deep.domain.generation import ScenePlanningBinding
from shader_deep.infrastructure.llm.client import build_model

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig

    from shader_deep.agents.generation.tools import RenderSession


def _prompt(session: RenderSession) -> str:
    prompt = MEMORY_PROMPT if session.memory is not None else SYSTEM_PROMPT
    if session.options.optimize_parameters:
        prompt = prompt.replace("finish_shader", "optimize_uniforms、finish_shader", 1)
        prompt += (
            "\n参数模式允许把连续量写成全局 uniform, 并随 render_shader 提交同名 parameter_specs (type、default、min、max 与可选 target_ref)。"
            "保持离散结构及公式在 GLSL 中。编译通过后可用 optimize_uniforms 锁定源码, 只放开目标相关参数并保护已匹配区域。"
            "structure_bottleneck 表示应检查结构。baseline_satisfied 表示起点已满足数值目标, 保留原候选供视觉判断。"
            "胜出候选须再次收到真实预览才可选择。"
        )
    if session.vista is not None:
        prompt = prompt.replace("只使用 render_shader", "只使用 list_records、read_record、render_shader", 1)
        prompt += "\n需要比较旧方案、查看历史调参或回滚来源时, 可按记录名回读本工作流归档的图像和 GLSL。"
    return prompt


def _run_config(session: RenderSession, usage: UsageMetadataCallbackHandler | None = None) -> RunnableConfig:
    # 用同一 session_id 关联多次 agent.invoke 的追踪记录与本地制品.
    # recursion_limit 是框架图步数上限, 不等于模型轮数或渲染次数预算.
    binding = session.state["tasks"][session.task_id].generation_binding
    source = (
        {
            "planning_sha256": binding.content_sha256,
            "observation_sha256": binding.observation_sha256,
            "sketch_id": binding.sketch_id,
            "alternative_index": binding.alternative,
            "planning_protocol": binding.schema_version,
        }
        if isinstance(binding, ScenePlanningBinding)
        else {}
    )
    return {
        "run_name": "shader-deep.generation",
        "tags": ["shader-deep", "generation"],
        "metadata": {
            **source,
            "session_id": session.run_dir.name,
            "task_id": session.task_id,
            "target_version": session.state["tasks"][session.task_id].target_version,
            "run_dir": str(session.run_dir),
            "width": session.options.width,
            "height": session.options.height,
            "time": session.options.time,
            "max_attempts": session.options.max_attempts,
            "max_model_calls": session.options.resolved_max_model_calls,
            "visual_memory": session.options.visual_memory,
            "generation_prompt_sha256": hashlib.sha256(_prompt(session).encode("utf-8")).hexdigest(),
        },
        "recursion_limit": 4 * session.options.resolved_max_model_calls + 10,
        "callbacks": [usage] if usage is not None else [],
        # 同批工具按响应顺序进入会话线程, 停止后的调用不能抢先渲染.
        "max_concurrency": 1,
    }


def _execute(session: RenderSession, *, model: BaseChatModel | None = None) -> None:
    # tools() 将绑定会话的方法包装成模型 Tool; 模型只需提交业务参数.
    tools = session.tools()
    # 比较流程共用一次构造的客户端配置, 每项仍创建独立 Agent 和消息历史.
    model = model if model is not None else build_model()
    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=_prompt(session),
        middleware=[
            # 有限短历史不触发额外摘要请求; 普通模型请求统一受本轮预算控制.
            SummarizationMiddleware(model=model, trigger=None),
            # 先加载最新材料, 再登记哪些成功预览将随本轮请求提供给模型.
            # lambda 每次取 session.state, 避免捕获后续已经过时的黑板快照.
            GenerationContextMiddleware(
                lambda: session.state,
                session.task_id,
                asset_root=session.asset_root,
                inputs=session.inputs,
                memory=session.memory,
                reference_copy=session.reference_copy,
            ),
            GenerationLoopMiddleware(session, tools),
        ],
    )
    messages = [HumanMessage(content="完成本次生成任务: 渲染并查看预览后用 finish_shader 选择版本; 无法继续时用 stop_generation 说明原因.")]
    # invoke 内部已包含模型与工具之间的循环. 外层循环处理模型只回复文字的情况:
    # 一次图执行后仍在运行, 就携带返回的历史继续, 总预算由 session 跨调用累计.
    usage = UsageMetadataCallbackHandler()
    try:
        while not session.is_finished:
            result = agent.invoke({"messages": messages}, config=_run_config(session, usage))
            messages = [
                *result["messages"],
                HumanMessage(content="本轮尚未结束. 请渲染或检查候选后选择; 无法继续时调用 stop_generation, 不自动切换方案."),
            ]
    finally:
        values = usage.usage_metadata.values()
        if values:
            session.token_usage = {
                "input_tokens": sum(value["input_tokens"] for value in values),
                "output_tokens": sum(value["output_tokens"] for value in values),
                "total_tokens": sum(value["total_tokens"] for value in values),
            }
