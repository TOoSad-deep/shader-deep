"""从多个独立视角分析 PNG, 并输出结构化 JSON 结果."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.types import AnalysisOptions


def main() -> int:
    """运行独立分析流程, 不启动渲染器.

    Returns:
        分析完整时返回 0, 部分完成或未完成时返回 1, 输入错误时返回 2.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("png", type=Path, help="Path to a local PNG reference")
    parser.add_argument("prompt", help="User requirements and analysis objective")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for the unique run folder")
    parser.add_argument("--max-tasks", type=int, default=6, help="Total lens tasks, including follow-ups and failures")
    parser.add_argument("--max-parallel", type=int, default=3, help="Maximum concurrent lens workers")
    parser.add_argument("--max-worker-calls", type=int, default=3, help="Model calls per lens worker")
    parser.add_argument("--max-main-calls", type=int, default=8, help="Coordinator model calls across planning and synthesis")
    parser.add_argument("--max-measurements", type=int, default=8, help="Unique coordinator measurements; cached results are free")
    parser.add_argument("--max-request-retries", type=int, default=2, help="Extra transient transport attempts per model call")
    parser.add_argument("--request-timeout-seconds", type=int, default=120, help="Network timeout for each model request attempt")
    parser.add_argument("--max-output-tokens", type=int, default=16384, help="Provider output token budget per request attempt")
    parser.add_argument(
        "--stream-model-responses",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use SSE transport; complete calls are validated before execution",
    )
    # 命令行参数先做基本类型转换, AnalysisOptions 再检查范围和布尔值等业务约束.
    args = parser.parse_args()
    try:
        options = AnalysisOptions(
            output_dir=args.output_dir,
            max_tasks=args.max_tasks,
            max_parallel=args.max_parallel,
            max_worker_calls=args.max_worker_calls,
            max_main_calls=args.max_main_calls,
            max_measurements=args.max_measurements,
            max_request_retries=args.max_request_retries,
            request_timeout_seconds=args.request_timeout_seconds,
            stream_model_responses=args.stream_model_responses,
            max_output_tokens=args.max_output_tokens,
        )
        outcome = run_analysis(args.png, args.prompt, options=options)
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stderr.write(f"Run: {outcome.run_dir}\nStatus: {outcome.stop_reason}\n")
    # 诊断信息写到 stderr; stdout 只输出一份 JSON, 便于脚本重定向和读取部分结果.
    result = asdict(outcome.summary_result) if outcome.summary_result is not None else None
    sys.stdout.write(
        json.dumps({"status": outcome.stop_reason, "run_dir": str(outcome.run_dir), "result": result}, ensure_ascii=False, indent=2) + "\n"
    )
    return 0 if outcome.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
