"""整合子 Agent 的显式交接、独立执行记录与失败制品."""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from shader_deep.agents.integration.comparison import ControlledSession
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.runtime.execution import AnalysisLimitError, AnalysisNoProgressError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig
    from pydantic import JsonValue

    from shader_deep.domain.library.materials import ComparisonManager
    from shader_deep.domain.library.models import VisualOutline
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
    from shader_deep.runtime.execution import AnalysisExecution
    from shader_deep.workflows.options import AnalysisOptions


@dataclass(frozen=True, kw_only=True)
class IntegrationInput:
    """只传入业务材料及受控库服务, 不传递主 Agent 的历史和修复状态."""

    state: BlackboardState
    parent_task_id: str
    outline_versions: tuple[VisualOutline, ...]
    issues: list[dict[str, object]]
    store: LibraryStore
    consumed_calls: int


@dataclass(frozen=True, kw_only=True)
class IntegrationOutcome:
    """整合角色返回的运行结果; 整轮交付状态仍由协调器计算."""

    outline_versions: tuple[VisualOutline, ...]
    resolutions: dict[str, dict[str, object]]
    gaps: list[dict[str, object]]
    comparisons: ComparisonManager
    finish_requested: bool
    phase_budgets: dict[str, dict[str, object]]
    execution: AnalysisExecution
    work_results: tuple[dict[str, object], ...]
    library_revision: int


class IntegrationWorker(ControlledSession, ABC):
    """独立维护整合历史、调用计数和草稿, 通过受控服务提交库更新."""

    def __init__(
        self,
        inputs: IntegrationInput,
        options: AnalysisOptions,
        directory: Path,
        reference_url: str,
        *,
        on_progress: Callable[[IntegrationOutcome], None] | None = None,
    ) -> None:
        """绑定本次同步委派的业务输入.

        Args:
            inputs: 探索终止后的业务快照及库服务.
            options: 原运行配置, 保留总预算与阶段预算.
            directory: 与主会话共用的制品目录, 子角色使用独立快照文件.
            reference_url: 启动时固定的原图字节.
            on_progress: 子角色检查点落盘后, 向协调器交付复制后的业务进度.
        """
        super().__init__(deepcopy(inputs.state), inputs.parent_task_id, options, directory, reference_url)
        self.agent_id = f"{inputs.parent_task_id}-integration"
        self.outline_versions = list(inputs.outline_versions)
        self.outline = self.outline_versions[-1]
        self.issues, self.store = deepcopy(inputs.issues), inputs.store
        self.consumed_calls = inputs.consumed_calls
        self.event = self.events.bind(self.agent_id, "integration", self.execution)
        self._started = False
        self._on_progress = on_progress

    def remaining_calls(self) -> int | None:
        """将初稿消耗计入总额度, 子角色的调用计数从零开始."""
        if not self.options.max_main_calls:
            return None
        return max(0, self.options.max_main_calls - self.consumed_calls - self.execution.model_calls)

    def config(self, task_id: str) -> RunnableConfig:
        """以独立执行身份追踪整合, 同时保留业务父任务关联."""
        config = super().config(task_id)
        config.update(run_name="shader-deep.integration", tags=["analysis", "integration"])
        config["metadata"] = {**config.get("metadata", {}), "task_id": self.agent_id, "parent_task_id": self.task_id}
        return config

    def _save_tool_result(self, record: dict[str, JsonValue]) -> None:
        super()._save_tool_result(
            {
                **record,
                "task_id": self.agent_id,
                "parent_task_id": self.task_id,
                "role": "integration",
                "global_model_call": self.consumed_calls + self.execution.model_calls,
            }
        )

    def result(self) -> IntegrationOutcome:
        """复制可变结果, 防止协调器继续使用子角色的运行中状态."""
        return IntegrationOutcome(
            outline_versions=tuple(self.outline_versions),
            resolutions=deepcopy(self.resolutions),
            gaps=deepcopy(self.gaps),
            comparisons=deepcopy(self.comparisons),
            finish_requested=self.finish_requested,
            phase_budgets=deepcopy(self.phase_budgets),
            execution=deepcopy(self.execution),
            work_results=tuple(deepcopy(self.work_results)),
            library_revision=self._require_store().revision,
        )

    def snapshot(self) -> dict[str, object]:
        """返回独立角色的可追溯状态, 不生成主任务的最终结果."""
        return {
            "task_id": self.agent_id,
            "parent_task_id": self.task_id,
            "role": "integration",
            "stop_reason": self.stop_reason,
            "execution": asdict(self.execution),
            "consumed_parent_calls": self.consumed_calls,
            "integration_stages": self.integration_cycles,
            "library_revision": self._require_store().revision,
            "outline_versions": [asdict(outline) for outline in self.outline_versions],
            "issue_resolutions": self.resolutions,
            "gaps": self.gaps,
            "integration": self.comparisons.snapshot(),
            "finish_requested": self.finish_requested,
            "phase_budgets": self.phase_budgets,
        }

    def save(self) -> None:
        """独立落盘后通知协调器更新主快照, 不共享模型历史和修复状态."""
        for name, payload in (("integration-subagent.json", self.snapshot()), ("integration-work.json", self.work_results)):
            destination = self.directory / name
            write_snapshot(destination, payload)
        if self._on_progress is not None:
            self._on_progress(self.result())

    def run(self, model: BaseChatModel) -> None:
        """同步执行一次整合; 中断和异常落盘后交由协调器保留部分交付.

        Args:
            model: 沿用当前配置的模型客户端.

        Raises:
            ValueError: 重复派发同一个整合实例.
        """
        if self._started:
            msg = "Integration subagent can only run once"
            raise ValueError(msg)
        self._started, self.execution.status = True, "running"
        self.event("subagent_started", {"parent_task_id": self.task_id})
        try:
            self._integrate(model)
            self.stop_reason = (
                "partial"
                if self.gaps
                or not self.finish_requested
                or self.comparisons.pending
                or self.comparisons.deferred
                or self.comparisons.active
                or any(not self._issue_resolved(str(issue["id"])) for issue in self.issues)
                else "completed"
            )
            self.execution.status = "completed"
        except KeyboardInterrupt:
            self.stop_reason, self.execution.status, self.execution.error = "interrupted", "stopped", "Interrupted by caller"
            raise
        except (AnalysisLimitError, AnalysisNoProgressError) as exc:
            self.stop_reason, self.execution.status, self.execution.error = "stopped", "stopped", str(exc)
            raise
        except Exception as exc:
            self.stop_reason, self.execution.status, self.execution.error = "error", "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.save()
            self.event("subagent_finished", {"parent_task_id": self.task_id, "status": self.stop_reason, "error": self.execution.error})

    @abstractmethod
    def _integrate(self, model: BaseChatModel) -> None:
        """由角色策略执行整合阶段."""
