"""一轮分析的业务与执行记录, 不持有角色的模型历史."""

from __future__ import annotations

import base64
import sys
from dataclasses import asdict
from threading import Event, Lock
from typing import TYPE_CHECKING

from shader_deep.domain.library.materials import ComparisonManager
from shader_deep.imaging.measurements import ReferenceMeasurements
from shader_deep.infrastructure.tracing import EventLog
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.workflows.outcomes import AnalysisOutcome

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig

    from shader_deep.domain.library.models import VisualOutline
    from shader_deep.domain.tasks import BlackboardState, ResultRecord
    from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
    from shader_deep.workflows.options import AnalysisOptions


from shader_deep.workflows.delivery import finalize_analysis
from shader_deep.workflows.progress import save_progress

PROTOCOL = "possibility_library_v1"


class AnalysisRun:
    """一轮分析的业务与执行记录, 不持有任何角色的模型历史或工具."""

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """绑定固定输入与运行目录.

        Args:
            state: 初始黑板.
            task_id: 主任务身份.
            options: 运行与请求预算.
            directory: 当前独立运行目录.
            reference_url: 启动时固定的原图内容.
        """
        self.state, self.task_id, self.options = state, task_id, options
        self.directory, self.reference_url = directory, reference_url
        self.execution = AnalysisExecution()
        self.workers: dict[str, AnalysisExecution] = {}
        self.summary_result: ResultRecord | None = None
        self.stop_reason = "running"
        self.outline: VisualOutline | None = None
        self.store: LibraryStore | None = None
        self.issues: list[dict[str, object]] = []
        self.resolutions: dict[str, dict[str, object]] = {}
        self.lock = Lock()
        self.cancelled = Event()
        self.last_action: dict[str, object] = {}
        self.events = EventLog(directory)
        self.event = self.events.bind(task_id, "coordinator", self.execution)
        self.measurements = ReferenceMeasurements(
            base64.b64decode(reference_url.split(",", 1)[1]), directory, task_id, state["tasks"][task_id].target_version, options.max_measurements
        )
        self.outline_versions: list[VisualOutline] = []
        self.directions: list[str] = []
        self.comparisons = ComparisonManager()
        self.finish_requested = False
        self.gaps: list[dict[str, object]] = []
        self.phase_budgets: dict[str, dict[str, object]] = {}

    @property
    def user_request(self) -> str:
        """返回原始要求与明确约束, 不混入模型判断."""
        target = self.state["targets"][self.state["tasks"][self.task_id].target_version]
        parts = [target.request]
        if target.constraints:
            parts.append("明确约束: " + "; ".join(target.constraints))
        if target.protected_features:
            parts.append("需保护特征: " + "; ".join(target.protected_features))
        objective = self.state["tasks"][self.task_id].objective
        if objective != target.request:
            parts.append("本次分析目标: " + objective)
        return "\n".join(parts)

    def config(self, task_id: str) -> RunnableConfig:
        """构造追踪配置, 子任务和主任务使用独立身份."""
        limit = self.options.max_main_calls if task_id == self.task_id else self.options.max_worker_calls
        return {
            "run_name": "shader-deep.exploration",
            "tags": ["analysis", "coordinator" if task_id == self.task_id else "explorer"],
            "metadata": {"task_id": task_id, "session_id": self.directory.name, "analysis_protocol": PROTOCOL},
            "recursion_limit": sys.maxsize if not limit else limit * 4 + 10,
        }

    def _require_store(self) -> LibraryStore:
        if self.store is None:
            msg = "Submit the visual outline first"
            raise ValueError(msg)
        return self.store

    def _uncovered(self) -> list[str]:
        library = self._require_store().library
        covered = {identity for sketch in library.sketch_library for identity in sketch.element_ids}
        covered.update(identity for feature in library.feature_library for identity in feature.element_ids)
        covered.update(endpoint.id for relation in library.relation_library for endpoint in relation.participants if endpoint.kind == "element")
        return [element.id for element in library.elements if element.id not in covered]

    def _issue_resolved(self, identity: str) -> bool:
        resolution = self.resolutions.get(identity, {})
        return resolution.get("disposition") == "dismissed" or (
            resolution.get("disposition") == "revised" and resolution.get("verified_outline_version") == len(self.outline_versions)
        )

    def _execution_snapshot(self) -> dict[str, object]:
        return {"main_execution": asdict(self.execution)}

    def outcome(self) -> AnalysisOutcome:
        """返回真实完成或部分交付状态, 保持公开入口包装类型."""
        return AnalysisOutcome(
            state=self.state, task_id=self.task_id, summary_result=self.summary_result, run_dir=self.directory, stop_reason=self.stop_reason
        )

    def save(self) -> None:
        """仅由工作流写主快照."""
        save_progress(self)

    def finalize(self) -> None:
        """统一检查整轮完成条件, 不由子角色声明交付成功."""
        finalize_analysis(self)
