"""兼容旧导入路径; 实现位于 infrastructure.llm.transport."""

from shader_deep.infrastructure.llm.transport import (
    MAX_CAUSE_DEPTH as MAX_CAUSE_DEPTH,
    RAW_TOOL_CALLS as RAW_TOOL_CALLS,
    AnalysisChatOpenAI as AnalysisChatOpenAI,
    Response as Response,
    _attempt as _attempt,
    _explicit_required as _explicit_required,
    _retryable as _retryable,
    call_with_recovery as call_with_recovery,
    configure_analysis_model as configure_analysis_model,
)
