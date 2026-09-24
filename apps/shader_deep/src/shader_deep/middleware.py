"""兼容旧导入路径; 实现位于 agents.generation.middleware."""

from shader_deep.agents.generation.middleware import (
    GenerationContextMiddleware as GenerationContextMiddleware,
    GenerationLoopMiddleware as GenerationLoopMiddleware,
)
