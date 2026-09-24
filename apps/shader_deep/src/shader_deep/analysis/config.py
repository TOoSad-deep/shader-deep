"""兼容旧导入路径; 实现位于 workflows.configuration."""

from shader_deep.workflows.configuration import (
    DEFAULT_CONFIG as DEFAULT_CONFIG,
    OUTPUT_FIELDS as OUTPUT_FIELDS,
    AnalysisPhase as AnalysisPhase,
    _output_path as _output_path,
    _read_values as _read_values,
    _with_sources as _with_sources,
    apply_analysis_overrides as apply_analysis_overrides,
    load_analysis_options as load_analysis_options,
    resolve_phase_options as resolve_phase_options,
)
