"""稳定的应用调用入口; 导入不创建模型、浏览器或运行目录."""

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.generation_comparison import GenerationComparison, GenerationPlan, PlanningGenerationComparison
from shader_deep.domain.local_probe import LocalProbeBinding, VerifiedSnippet
from shader_deep.domain.review_actions import ReviewActionSummary
from shader_deep.domain.scene import SceneElementSource
from shader_deep.domain.shader_parameters import ParameterSpec
from shader_deep.domain.visual_review import (
    BoundReviewCriteria,
    DimensionCriteria,
    PixelRegion,
    RequiredRegion,
    ReviewCoverage,
    ReviewRequest,
    ReviewSample,
)
from shader_deep.infrastructure.storage.generation_comparison import read_generation_comparison, select_generation_comparison
from shader_deep.infrastructure.storage.observation_package import read_observation_package
from shader_deep.infrastructure.storage.planning_package import read_planning_package, read_planning_sketch
from shader_deep.infrastructure.storage.report_package import read_report_package, read_sketch
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.analysis import run_analysis, run_analysis_task
from shader_deep.workflows.generation import GenerationIncompleteError, generate_shader, generate_task, run_generation, run_shader
from shader_deep.workflows.generation_comparison import run_generation_comparison, run_planning_generation_comparison
from shader_deep.workflows.generation_from_planning import run_generation_from_planning
from shader_deep.workflows.generation_from_report import run_generation_from_report
from shader_deep.workflows.optimization_workflow import GateADecision, GateBDecision, WorkflowState, advance_workflow, read_workflow, start_workflow
from shader_deep.workflows.options import AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome
from shader_deep.workflows.planning import PlanningOptions, PlanningOutcome, run_planning
from shader_deep.workflows.review import ReviewOptions, ReviewOutcome, freeze_workflow_review, republish_review, run_review, run_workflow_review
from shader_deep.workflows.scene_generation import run_scene_generation

__all__ = [
    "AnalysisOptions",
    "AnalysisOutcome",
    "BoundReviewCriteria",
    "DimensionCriteria",
    "GateADecision",
    "GateBDecision",
    "GenerationComparison",
    "GenerationIncompleteError",
    "GenerationOptions",
    "GenerationOutcome",
    "GenerationPlan",
    "LocalProbeBinding",
    "ParameterSpec",
    "PixelRegion",
    "PlanningGenerationComparison",
    "PlanningOptions",
    "PlanningOutcome",
    "RequiredRegion",
    "ReviewActionSummary",
    "ReviewCoverage",
    "ReviewOptions",
    "ReviewOutcome",
    "ReviewRequest",
    "ReviewSample",
    "SceneElementSource",
    "VerifiedSnippet",
    "WorkflowState",
    "advance_workflow",
    "freeze_workflow_review",
    "generate_shader",
    "generate_task",
    "read_generation_comparison",
    "read_observation_package",
    "read_planning_package",
    "read_planning_sketch",
    "read_report_package",
    "read_review_package",
    "read_sketch",
    "read_workflow",
    "republish_review",
    "run_analysis",
    "run_analysis_task",
    "run_generation",
    "run_generation_comparison",
    "run_generation_from_planning",
    "run_generation_from_report",
    "run_planning",
    "run_planning_generation_comparison",
    "run_review",
    "run_scene_generation",
    "run_shader",
    "run_workflow_review",
    "select_generation_comparison",
    "start_workflow",
]
