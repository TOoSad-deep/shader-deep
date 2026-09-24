"""显式执行固定 W1/W2/W3 的有界去重实验, 不接管默认分析入口."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.analysis.config import apply_analysis_overrides, load_analysis_options
from shader_deep.analysis.controlled import ControlledSession, ControlledWork
from shader_deep.analysis.exploration import validate_library
from shader_deep.analysis.replay import _prepare_replay
from shader_deep.config import build_model

if TYPE_CHECKING:
    from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome

_F1 = "run-avu0ozrm-a002-report::f_card_shadow"
_F2 = "run-avu0ozrm-a005-report::f_card_shadow"
_F3 = "run-avu0ozrm-a002-report::f_field_vignette"
_F4 = "run-avu0ozrm-a005-report::f_bg_halo"
_CANDIDATES = (
    "run-avu0ozrm-a002-report::cs1",
    "run-avu0ozrm-a002-report::cs2",
    "run-avu0ozrm-a005-report::c_shadow_blur_copy",
    "run-avu0ozrm-a005-report::c_shadow_gradient_ring",
)


def fixed_works() -> tuple[ControlledWork, ...]:
    """返回固定真实样本身份, 不预先规定模型必须合并哪些机制.

    Returns:
        特征、依赖拥有者合并的候选、独立特征三个工作.
    """
    return (
        ControlledWork(id="W1", kind="feature", target_ids=(_F1, _F2), question="比较两条卡片投影外观是否等价; 说明依据后合并、保留或暂缓."),
        ControlledWork(
            id="W2",
            kind="candidate",
            target_ids=_CANDIDATES,
            owner_ids=(_F1, _F2),
            depends_on="W1",
            question="逐一比较四个候选的形成机制; 只有机制等价才合并, 不因外观相似而吞并不同机制; 明确保留其余目标或暂缓整组.",
        ),
        ControlledWork(
            id="W3", kind="feature", target_ids=(_F3, _F4), question="比较两条底场晕影外观的范围及描述是否等价; 说明依据后合并、保留或暂缓."
        ),
    )


def _extend_manifest(session: ControlledSession, works: tuple[ControlledWork, ...], max_work_calls: int, model_name: str | None) -> None:
    """在现有 manifest 补充本实验的身份和有效调用上限, 不保存凭据."""
    destination = session.directory / "replay-manifest.json"
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload.update(
        kind="controlled_deduplication_replay",
        model_name=model_name,
        controlled_works=[asdict(work) for work in works],
        controlled_budgets={"max_main_calls": session.options.max_main_calls, "max_work_calls": max_work_calls},
    )
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_summary(session: ControlledSession, works: tuple[ControlledWork, ...]) -> None:
    """分别记录三个步骤的实际回执; 局部流程通过不代表语义或整轮完成."""
    results = {result["work_id"]: result for result in session.work_results}
    steps = [
        results.get(work.id, {"work_id": work.id, "status": "not_run", "reason": session.execution.error or session.stop_reason, "model_calls": 0})
        for work in works
    ]
    store = session._require_store()
    try:
        validate_library(store.library)
        validation: dict[str, object] = {"valid": True}
    except ValueError as exc:
        validation = {"valid": False, "error": str(exc)}
    payload = {
        "status": session.stop_reason,
        "error": session.execution.error,
        "steps": steps,
        "three_step_flow_passed": (
            steps[0]["status"] == "modified"
            and all(step["status"] in {"modified", "preserved"} for step in steps[1:])
            and all(step.get("receipt") and step.get("presented_versions") for step in steps)
        ),
        "semantic_review": "not_performed",
        "model_calls": session.execution.model_calls,
        "library_validation": validation,
        "final_revision": store.revision,
        "aliases": store.aliases,
        "initial_issue_count": len(session.issues),
        "failed_worker_ids": [identity for identity, worker in session.workers.items() if worker.status != "completed"],
    }
    (session.directory / "controlled-summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_controlled_replay(source: Path, *, options: AnalysisOptions, max_work_calls: int = 4) -> AnalysisOutcome:
    """从完整冻结报告执行一次固定清单, 始终保留部分结果与失败义务.

    Args:
        source: 含原始报告、任务、问题及原图的来源运行目录.
        options: 显式正数总调用上限及本次输出参数.
        max_work_calls: 每项工作的调用上限, 包含局部修复.

    Returns:
        保留全运行真实状态和独立制品目录的结果.

    Raises:
        ValueError: 总调用或每项工作额度不是正数.
    """
    if options.max_main_calls < 1 or isinstance(max_work_calls, bool) or not isinstance(max_work_calls, int) or max_work_calls < 1:
        msg = "Controlled replay requires positive main-call and work-call budgets"
        raise ValueError(msg)
    works = fixed_works()
    session = _prepare_replay(source, options=options, session_type=ControlledSession)
    _extend_manifest(session, works, max_work_calls, None)
    try:
        model = build_model()
        _extend_manifest(session, works, max_work_calls, model.model_name)
        session.run_controlled(model, works, max_work_calls=max_work_calls)
    except KeyboardInterrupt:
        session.stop_reason, session.execution.status, session.execution.error = "interrupted", "stopped", "Interrupted by caller"
        raise
    except Exception as exc:  # noqa: BLE001  # 与原回放入口一致, 保存服务或业务失败的真实制品.
        session.stop_reason, session.execution.status, session.execution.error = "error", "failed", f"{type(exc).__name__}: {exc}"
    finally:
        session.finalize()
        session.save()
        _write_summary(session, works)
        session.event("run_finished", {"status": session.stop_reason, "error": session.execution.error})
    return session.outcome()


def main() -> int:
    """启动一次显式实验; 预算默认值仅作用于本入口."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--max-main-calls", type=int, default=12)
    parser.add_argument("--max-work-calls", type=int, default=4)
    parser.add_argument("--integration-max-output-tokens", type=int)
    args = parser.parse_args()
    if args.max_main_calls < 1 or args.max_work_calls < 1:
        parser.error("Controlled replay call budgets must be positive")
    try:
        overrides = {"output_dir": args.output_dir, "max_main_calls": args.max_main_calls}
        if args.integration_max_output_tokens is not None:
            overrides["integration_max_output_tokens"] = args.integration_max_output_tokens
        options = apply_analysis_overrides(load_analysis_options(args.config), overrides)
        outcome = run_controlled_replay(args.source, options=options, max_work_calls=args.max_work_calls)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps({"status": outcome.stop_reason, "run_dir": str(outcome.run_dir)}, ensure_ascii=False) + "\n")
    return 0 if outcome.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
