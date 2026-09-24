"""兼容旧导入路径; 实现位于 runtime.runner."""

from shader_deep.runtime.runner import (
    FORMAT_ERRORS as FORMAT_ERRORS,
    MAX_FORMAT_REPAIR_CALLS as MAX_FORMAT_REPAIR_CALLS,
    AnalysisLoop as AnalysisLoop,
    _complete_response as _complete_response,
)
