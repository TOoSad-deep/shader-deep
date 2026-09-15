"""按 Agent 组织上下文构造, 保留统一的公开导入入口."""

# 对外导出保持稳定: 调用方仍可 from shader_deep.context import build_generation_context.
# 实际生成策略位于 generation.py; 新角色实现后再增加其对应模块和导出.

from shader_deep.context.common import png_data_url
from shader_deep.context.generation import GenerationContext, build_generation_context

__all__ = ["GenerationContext", "build_generation_context", "png_data_url"]
