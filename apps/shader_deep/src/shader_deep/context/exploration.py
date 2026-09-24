"""兼容旧导入路径; 实现位于 agents.exploration.context."""

from shader_deep.agents.exploration.context import build_exploration_context as build_exploration_context
from shader_deep.agents.outline.context import build_outline_context as build_outline_context
from shader_deep.compatibility.ondemand.context import build_integration_context as build_integration_context
from shader_deep.infrastructure.llm.messages import _message as _message
