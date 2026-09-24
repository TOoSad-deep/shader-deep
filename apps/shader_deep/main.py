"""根据 PNG 参考图和文字提示生成兼容 ShaderToy 的 GLSL."""  # 保留独立脚本入口.

# 保留原脚本的导入名称, 实现统一放在应用包中.
from shader_deep.agents.generation.prompts import SYSTEM_PROMPT as SYSTEM_PROMPT
from shader_deep.cli.generation import main as main, parse_args as parse_args
from shader_deep.infrastructure.llm.client import build_model as build_model, require_env as require_env
from shader_deep.workflows.generation import (
    generate_shader as generate_shader,
    normalize_glsl as normalize_glsl,
    png_data_url as png_data_url,
)

if __name__ == "__main__":
    raise SystemExit(main())
