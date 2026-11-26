"""启动、检查或继续带人工门禁的 PNG 转 Shader 流程."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.infrastructure.storage.observation_package import read_observation_package
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.optimization_workflow import GateADecision, GateBDecision, advance_workflow, read_workflow, start_workflow
from shader_deep.workflows.optimization_workflow.storage import _asset

if TYPE_CHECKING:
    from shader_deep.workflows.optimization_workflow import WorkflowState


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="运行观察、规划与评审标准草案整理")
    start.add_argument("png", type=Path)
    start.add_argument("request")
    start.add_argument("--output-dir", type=Path)
    start.add_argument("--max-rounds", type=int, default=4)
    start.add_argument("--max-model-calls", type=int)
    start.add_argument("--max-output-tokens", type=int, help="所有模型请求共用的输出 token 总预算")
    start.add_argument("--max-total-tokens", type=int, help="可选的输入与输出 token 总预算")
    start.add_argument("--model-context-tokens", type=int, help="所有参与模型的单请求上下文硬上界, 配合总量预算")
    status = commands.add_parser("status", help="读取门禁状态、预算与制品位置")
    status.add_argument("directory", type=Path)
    panel = commands.add_parser("panel", help="核对门禁的观察依据、方案或候选评审测量")
    panel.add_argument("directory", type=Path)
    advance = commands.add_parser("advance", help="读取人工决策 JSON 并执行当前门禁")
    advance.add_argument("directory", type=Path)
    advance.add_argument("decision_file", type=Path)
    retry = commands.add_parser("retry-criteria", help="用原观察、规划和剩余额度重试首次标准整理或重拟门禁 A 草案")
    retry.add_argument("directory", type=Path)
    retry.add_argument("--reason", help="门禁 A 要求重新草拟时必填。说明当前草案的问题")
    return parser.parse_args()


def _panel(state: WorkflowState) -> dict[str, object]:
    root = Path(state.directory)
    summary = dict(state.model_dump(mode="json", exclude={"candidates", "observation_versions", "attempts"}))
    if state.observation_path is not None and state.phase == "gate_a":
        manifest, observations = read_observation_package(_asset(root, state.observation_path))
        summary["observations"] = observations.model_dump(mode="json")
        summary["observation_gaps"] = [row.model_dump(mode="json") for row in manifest.gaps]
        summary["reference_measurements"] = [row.model_dump(mode="json") for row in manifest.reference_measurements]
        if state.planning_path:
            summary["sketches"] = json.loads((_asset(root, state.planning_path) / "sketches.json").read_bytes())
        if state.criteria_path:
            summary["criteria"] = json.loads(_asset(root, state.criteria_path).read_bytes())
        if state.comparison_path:
            summary["comparison"] = json.loads(_asset(root, state.comparison_path).read_bytes())
    current = state.current_candidate
    if current is not None and current.review_path and state.phase == "gate_b":
        review = read_review_package(_asset(root, current.review_path)).report
        summary["review_summary"] = review.action_summary.model_dump(mode="json")
        summary["pixel_measurements"] = (
            [{**row.model_dump(mode="json"), "luma_difference": row.rendered.mean_luma - row.reference.mean_luma} for row in review.evidence.samples]
            if review.evidence is not None
            else []
        )
        if current.diagnosis_path:
            summary["diagnosis"] = json.loads(_asset(root, current.diagnosis_path).read_bytes())
    return summary


def main() -> int:
    """执行一次明示的人机决策步骤, 将 JSON 状态写入 stdout."""
    args = _arguments()
    try:
        if args.command == "start":
            limits = {
                key: value
                for key in ("max_model_calls", "max_output_tokens", "max_total_tokens", "model_context_tokens")
                if (value := getattr(args, key)) is not None
            }
            result = start_workflow(args.png, args.request, output_dir=args.output_dir, max_rounds=args.max_rounds, **limits)
        else:
            result = read_workflow(args.directory)
            if args.command == "panel":
                sys.stdout.write(json.dumps(_panel(result), indent=2, ensure_ascii=False) + "\n")
                return 1 if result.phase == "blocked" else 0
            if args.command == "advance":
                schema = GateADecision if result.phase in {"gate_a", "blocked"} else GateBDecision
                decision = schema.model_validate_json(args.decision_file.read_bytes())
                if decision.expected_revision is None:
                    decision = decision.model_copy(update={"expected_revision": result.revision})
                result = advance_workflow(args.directory, decision)
            elif args.command == "retry-criteria":
                result = advance_workflow(
                    args.directory, GateADecision(action="retry_criteria", reason=args.reason, expected_revision=result.revision)
                )
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    return 1 if result.phase == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
