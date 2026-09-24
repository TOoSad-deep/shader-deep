"""兼容旧导入路径; 实现位于 runtime.execution."""

from shader_deep.runtime.execution import (
    AnalysisExecution as AnalysisExecution,
    AnalysisLimitError as AnalysisLimitError,
    AnalysisNoProgressError as AnalysisNoProgressError,
    RequestAttempt as RequestAttempt,
    ToolFeedback as ToolFeedback,
)
from shader_deep.workflows.options import MIN_ANALYSIS_TASKS as MIN_ANALYSIS_TASKS, AnalysisOptions as AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome as AnalysisOutcome
