"""兼容旧导入路径; 实现位于 infrastructure.llm.client."""

from shader_deep.agents.generation.options import GenerationOptions as GenerationOptions
from shader_deep.infrastructure.llm.client import build_model as build_model, require_env as require_env
