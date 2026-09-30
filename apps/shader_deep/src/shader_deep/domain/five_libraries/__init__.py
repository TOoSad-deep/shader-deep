"""五库领域入口: 字段、校验、来源汇集与保义引用维护."""

from shader_deep.domain.five_libraries.models import (
    Alternative,
    Element,
    ExplorationSubmission,
    Feature,
    FiveLibraries,
    Issue,
    Mechanism,
    MechanismRef,
    MergeGroup,
    MergeProposal,
    Participant,
    Relation,
    Sketch,
)
from shader_deep.domain.five_libraries.operations import apply_merges, collect_explorations, exploration_mappings, merge_mapping, rewrite_issues
from shader_deep.domain.five_libraries.references import LibraryValidationError, parse_references, resolve_mapping, rewrite_text
from shader_deep.domain.five_libraries.validation import object_index, validate_exploration, validate_libraries

__all__ = [
    "Alternative",
    "Element",
    "ExplorationSubmission",
    "Feature",
    "FiveLibraries",
    "Issue",
    "LibraryValidationError",
    "Mechanism",
    "MechanismRef",
    "MergeGroup",
    "MergeProposal",
    "Participant",
    "Relation",
    "Sketch",
    "apply_merges",
    "collect_explorations",
    "exploration_mappings",
    "merge_mapping",
    "object_index",
    "parse_references",
    "resolve_mapping",
    "rewrite_issues",
    "rewrite_text",
    "validate_exploration",
    "validate_libraries",
]
