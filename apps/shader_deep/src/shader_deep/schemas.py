"""兼容旧导入路径; 实现位于 domain.tasks."""

from shader_deep.domain.tasks import (
    AgentRole as AgentRole,
    BlackboardState as BlackboardState,
    CandidateRecord as CandidateRecord,
    ResultRecord as ResultRecord,
    ResultStatus as ResultStatus,
    TargetRecord as TargetRecord,
    TaskRecord as TaskRecord,
    TaskRecords as TaskRecords,
)
