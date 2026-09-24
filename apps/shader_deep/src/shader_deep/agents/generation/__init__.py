"""生成角色; 旧公开函数延迟转发到工作流, 避免角色装配循环依赖."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shader_deep.agents.generation.agent import _execute as _execute, _run_config as _run_config
    from shader_deep.agents.generation.prompts import SYSTEM_PROMPT as SYSTEM_PROMPT
    from shader_deep.workflows.generation import (
        GenerationIncompleteError as GenerationIncompleteError,
        _completed_code as _completed_code,
        generate_shader as generate_shader,
        generate_task as generate_task,
        normalize_glsl as normalize_glsl,
        run_generation as run_generation,
        run_shader as run_shader,
    )


def __getattr__(name: str) -> object:
    """按旧名称读取生成工作流入口."""
    module = {
        "SYSTEM_PROMPT": "shader_deep.agents.generation.prompts",
        "_execute": "shader_deep.agents.generation.agent",
        "_run_config": "shader_deep.agents.generation.agent",
    }.get(name, "shader_deep.workflows.generation")
    return getattr(import_module(module), name)
