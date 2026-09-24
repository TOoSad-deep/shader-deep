"""从多个独立视角分析 PNG, 并输出结构化 JSON 结果."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, fields
from pathlib import Path

from shader_deep.api import run_analysis
from shader_deep.domain.library.models import PossibilityLibrary, compact_data
from shader_deep.workflows.configuration import apply_analysis_overrides, load_analysis_options
from shader_deep.workflows.options import AnalysisOptions


def main() -> int:
    """运行独立分析流程, 不启动渲染器.

    Returns:
        分析完整时返回 0, 部分完成或未完成时返回 1, 输入错误时返回 2.
    """
    parser = argparse.ArgumentParser(description=__doc__, argument_default=argparse.SUPPRESS)
    parser.add_argument("png", type=Path, help="Path to a local PNG reference")
    parser.add_argument("prompt", help="User requirements and analysis objective")
    parser.add_argument("--config", type=Path, help="Analysis YAML path (default: analysis.yaml bundled with shader_deep.resources)")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for the unique run folder")
    parser.add_argument("--max-tasks", type=int, help="Total lens tasks, including follow-ups and failures")
    parser.add_argument("--max-parallel", type=int, help="Maximum concurrent lens workers")
    parser.add_argument("--max-worker-calls", type=int, help="Model calls per lens worker; 0 means unlimited")
    parser.add_argument("--max-main-calls", type=int, help="Coordinator model calls across planning and synthesis; 0 means unlimited")
    parser.add_argument("--max-repeated-no-progress", type=int, help="Stop after identical rejected turns without new material; 0 disables")
    parser.add_argument("--max-measurements", type=int, help="Unique coordinator measurements; cached results are free")
    parser.add_argument("--max-request-retries", type=int, help="Extra transient transport attempts per model call")
    parser.add_argument("--request-timeout-seconds", type=int, help="Network timeout for each model request attempt")
    parser.add_argument("--max-output-tokens", type=int, help="Provider output token budget per request attempt")
    for stage in ("outline", "integration", "worker"):
        parser.add_argument(f"--{stage}-max-output-tokens", type=int, help=f"Provider output token budget for {stage}; overrides generic CLI budget")
    parser.add_argument(
        "--stream-model-responses",
        action=argparse.BooleanOptionalAction,
        help="Use SSE transport; complete calls are validated before execution",
    )
    # 仅显式 CLI 参数覆盖 YAML, 避免 argparse 默认值悄悄覆盖文件中的预算.
    args = parser.parse_args()
    try:
        options = load_analysis_options(getattr(args, "config", None))
        overrides = {field.name: getattr(args, field.name) for field in fields(AnalysisOptions) if hasattr(args, field.name)}
        options = apply_analysis_overrides(options, overrides)
        outcome = run_analysis(args.png, args.prompt, options=options)
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stderr.write(f"Run: {outcome.run_dir}\nStatus: {outcome.stop_reason}\n")
    # 诊断信息写到 stderr; stdout 只输出一份 JSON, 便于脚本重定向和读取部分结果.
    result = asdict(outcome.summary_result) if outcome.summary_result is not None else None
    if result is not None and outcome.summary_result is not None and isinstance(outcome.summary_result.analysis_detail, PossibilityLibrary):
        result["analysis_detail"] = compact_data(outcome.summary_result.analysis_detail)
    sys.stdout.write(
        json.dumps({"status": outcome.stop_reason, "run_dir": str(outcome.run_dir), "result": result}, ensure_ascii=False, indent=2) + "\n"
    )
    return 0 if outcome.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
