"""工作流拥有的主运行快照, 汇总各角色的独立进度."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from shader_deep.domain.library.models import compact_data
from shader_deep.infrastructure.storage.artifacts import save_run

if TYPE_CHECKING:
    from shader_deep.workflows.state import AnalysisRun

PROTOCOL = "possibility_library_v1"


def save_progress(run: AnalysisRun) -> None:
    """保存派生运行记录; 库版本与批量回执以库快照为权威."""
    options = {**asdict(run.options), "output_dir": str(run.options.output_dir) if run.options.output_dir else None}
    save_run(
        run.directory,
        run.state,
        {
            "kind": "independent_exploration",
            "analysis_protocol": PROTOCOL,
            "task_id": run.task_id,
            "options": options,
            "phase_budgets": run.phase_budgets,
            "stop_reason": run.stop_reason,
            **run._execution_snapshot(),
            "worker_executions": {key: asdict(value) for key, value in run.workers.items()},
            "visual_outline": compact_data(run.outline) if run.outline else None,
            "outline_versions": [compact_data(value) for value in run.outline_versions],
            "issues": run.issues,
            "issue_resolutions": run.resolutions,
            "gaps": run.gaps,
            "integration": run.comparisons.snapshot(),
            "summary_result_id": run.summary_result.id if run.summary_result else None,
            "reference_sha256": run.measurements.image_sha256,
            "presented_library_items": sorted(run.store.presented) if run.store else [],
        },
    )
