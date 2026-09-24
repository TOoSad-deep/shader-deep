"""兼容旧导入路径; 实现位于 agents.exploration.agent."""

from shader_deep.agents.exploration.agent import (
    _check_cancelled as _check_cancelled,
    _check_worker_request as _check_worker_request,
    _control_feedback as _control_feedback,
    _event_callback as _event_callback,
    _issue_tool as _issue_tool,
    _outcome as _outcome,
    _record_output_budget as _record_output_budget,
    _stop_tool as _stop_tool,
    run_exploration as run_exploration,
)
from shader_deep.agents.exploration.contracts import ExplorationOutcome as ExplorationOutcome, OutlineIssue as OutlineIssue
