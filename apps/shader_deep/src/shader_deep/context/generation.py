"""兼容旧导入路径; 实现位于 agents.generation.context."""

from shader_deep.agents.generation.context import (
    GenerationContext as GenerationContext,
    _candidate_data as _candidate_data,
    _candidates as _candidates,
    _images as _images,
    _message as _message,
    _metadata as _metadata,
    _result_data as _result_data,
    build_generation_context as build_generation_context,
)
