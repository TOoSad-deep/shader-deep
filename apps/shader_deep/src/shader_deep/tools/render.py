"""兼容旧导入路径; 实现位于 agents.generation.tools.render."""

from shader_deep.agents.generation.tools.render import _render_png as _render_png, record_candidate, render_candidate as render_candidate

_record = record_candidate
