"""兼容旧导入路径; 实现位于 domain.legacy."""

from shader_deep.domain.errors import AnalysisValidationError as AnalysisValidationError
from shader_deep.domain.legacy import (
    AnalysisSummary as AnalysisSummary,
    AnalysisTaskRequest as AnalysisTaskRequest,
    HypothesisLink as HypothesisLink,
    ImplementationSketch as ImplementationSketch,
    Interpretation as Interpretation,
    LensConfig as LensConfig,
    LensDraft as LensDraft,
    LensReport as LensReport,
    Observation as Observation,
    RenderCheckpoint as RenderCheckpoint,
    SourcedStatement as SourcedStatement,
    SourceKind as SourceKind,
    SourceRef as SourceRef,
    VisualDecomposition as VisualDecomposition,
    VisualElement as VisualElement,
    VisualEvidence as VisualEvidence,
    VisualFeature as VisualFeature,
    VisualMapping as VisualMapping,
    VisualRelation as VisualRelation,
    VisualRevision as VisualRevision,
    _interpretation_reference_issues as _interpretation_reference_issues,
)
from shader_deep.domain.primitives import CONFIG as CONFIG, Text as Text
