"""兼容旧导入路径; 实现位于 agents.integration.agent."""

from shader_deep.agents.integration.agent import IntegrationSubagent as IntegrationSubagent
from shader_deep.agents.integration.stages import (
    DISCOVERY_PROMPT as DISCOVERY_PROMPT,
    ISSUE_PROMPT as ISSUE_PROMPT,
    MIN_COMPARISON_TARGETS as MIN_COMPARISON_TARGETS,
    VERIFY_PROMPT as VERIFY_PROMPT,
    CandidatePlan as CandidatePlan,
    OwnerPlan as OwnerPlan,
    PlannedCandidates as PlannedCandidates,
    PlannedComparison as PlannedComparison,
)
