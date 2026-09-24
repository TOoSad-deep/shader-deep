"""稳定的应用调用入口; 导入不创建模型、浏览器或运行目录."""

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.workflows.analysis import run_analysis, run_analysis_task
from shader_deep.workflows.generation import GenerationIncompleteError, generate_shader, generate_task, run_generation, run_shader
from shader_deep.workflows.options import AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome

__all__ = [
    "AnalysisOptions",
    "AnalysisOutcome",
    "GenerationIncompleteError",
    "GenerationOptions",
    "GenerationOutcome",
    "generate_shader",
    "generate_task",
    "run_analysis",
    "run_analysis_task",
    "run_generation",
    "run_shader",
]
