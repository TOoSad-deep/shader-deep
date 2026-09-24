"""主分析协调器: 初稿、独立探索、同步委派整合子 Agent 与最终交付."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.integration_agent import IntegrationSubagent
from shader_deep.analysis.integration_worker import IntegrationInput

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.analysis.integration_worker import IntegrationOutcome
    from shader_deep.analysis.types import AnalysisOptions
    from shader_deep.schemas import BlackboardState


class ManagedIntegrationSession(ExplorationSession):
    """主会话只编排整合子任务, 不持有其模型历史、工具或修复会话."""

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """保留原入口参数, 为每次运行建立独立子角色槽位.

        Args:
            state: 初始业务黑板.
            task_id: 主分析任务身份.
            options: 原运行配置与预算.
            directory: 当前运行目录.
            reference_url: 启动时固定的原图内容.
        """
        super().__init__(state, task_id, options, directory, reference_url)
        self.integration_agent: IntegrationSubagent | None = None
        self.integration_result: IntegrationOutcome | None = None
        self._integration_parent_gaps: list[dict[str, object]] = []

    def _integrate(self, model: BaseChatModel) -> None:
        if self.integration_agent is not None:
            msg = "Integration has already been dispatched for this run"
            raise ValueError(msg)
        inputs = IntegrationInput(
            state=self.state,
            parent_task_id=self.task_id,
            outline_versions=tuple(self.outline_versions),
            issues=self.issues,
            store=self._require_store(),
            consumed_calls=self.execution.model_calls,
        )
        self._integration_parent_gaps = list(self.gaps)
        self.integration_agent = IntegrationSubagent(
            inputs, self.options, self.directory, self.reference_url, on_progress=self._update_integration_progress
        )
        self.save()
        try:
            self.integration_agent.run(model)
        finally:
            self._accept_integration(self.integration_agent.result())
            self.save()
        self.finalize()

    def _update_integration_progress(self, result: IntegrationOutcome) -> None:
        self._accept_integration(result)
        self.save()

    def _accept_integration(self, result: IntegrationOutcome) -> None:
        self.integration_result = result
        self.outline_versions = list(result.outline_versions)
        self.outline = self.outline_versions[-1]
        self.resolutions = result.resolutions
        # 每次回调携带完整子角色快照, 替换上次进度而非重复追加缺口.
        self.gaps = [*self._integration_parent_gaps, *result.gaps]
        self.comparisons = result.comparisons
        self.finish_requested = result.finish_requested
        self.phase_budgets.update(result.phase_budgets)

    def _execution_snapshot(self) -> dict[str, object]:
        coordinator = self.execution
        if self.integration_agent is None:
            return {**super()._execution_snapshot(), "coordinator_execution": asdict(coordinator), "integration_execution": None}
        child = self.integration_agent.execution
        offset = coordinator.model_calls
        # main_execution 保留原累计口径; 独立计数和错误另外保存, 不反向污染主角色历史.
        aggregate = replace(
            coordinator,
            status=child.status if child.status in {"failed", "stopped"} else "running" if self.stop_reason == "running" else coordinator.status,
            model_calls=offset + child.model_calls,
            request_attempts=coordinator.request_attempts
            + tuple(replace(attempt, model_call=offset + attempt.model_call) for attempt in child.request_attempts),
            tool_feedback=coordinator.tool_feedback
            + tuple(replace(feedback, model_call=offset + feedback.model_call) for feedback in child.tool_feedback),
            format_repair_calls=coordinator.format_repair_calls + child.format_repair_calls,
            format_repair_scopes={**coordinator.format_repair_scopes, **child.format_repair_scopes},
            format_repair_resolved_through={
                **coordinator.format_repair_resolved_through,
                **{scope: len(coordinator.tool_feedback) + index for scope, index in child.format_repair_resolved_through.items()},
            },
        )
        return {
            "main_execution": asdict(aggregate),
            "coordinator_execution": asdict(coordinator),
            "integration_execution": asdict(child),
            "integration_task_id": self.integration_agent.agent_id,
        }
