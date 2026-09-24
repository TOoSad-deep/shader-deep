"""旧上下文导入入口; 角色上下文位于 agents, 消息编码位于 infrastructure."""

from shader_deep.agents.generation.context import GenerationContext, build_generation_context
from shader_deep.infrastructure.llm.messages import png_data_url

__all__ = ["GenerationContext", "build_generation_context", "png_data_url"]
