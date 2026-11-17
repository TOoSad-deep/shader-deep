"""从已发布全图观察运行单个 M/S 规划 Agent."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.infrastructure.analysis_logging import analysis_console
from shader_deep.infrastructure.storage.planning_inputs import package_asset, reference_size
from shader_deep.workflows.planning import PlanningOptions, run_planning
from shader_deep.workflows.planning.configuration import apply_planning_overrides, load_planning_options


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, argument_default=argparse.SUPPRESS)
    parser.add_argument("observation", type=Path, help="已发布的 scene_observations_v1 观察包目录")
    parser.add_argument("request", help="本次整图规划要求")
    parser.add_argument("--config", type=Path, help="规划 YAML 路径")
    parser.add_argument("--output-dir", type=Path, help="独立运行目录的父目录")
    parser.add_argument("--max-model-calls", type=int, help="有限主模型调用预算, 默认 20")
    parser.add_argument("--max-request-retries", type=int, help="暂时性请求错误的额外重试次数")
    parser.add_argument("--request-timeout-seconds", type=int, help="连接、读取等传输操作的超时秒数")
    parser.add_argument("--max-output-tokens", type=int, help="每次请求的输出 token 预算, 默认 32768")
    parser.add_argument("--stream-model-responses", action=argparse.BooleanOptionalAction, help="使用完整接收后校验的流式请求")
    parser.add_argument("--width", type=int, help="预期渲染宽度, 默认原图宽度")
    parser.add_argument("--height", type=int, help="预期渲染高度, 默认原图高度")
    parser.add_argument("--time", type=float, help="预期 iTime 秒数, 默认 0")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="stderr 诊断级别")
    return parser


def _generation_options(args: argparse.Namespace) -> GenerationOptions | None:
    if not any(hasattr(args, name) for name in ("width", "height", "time")):
        return None
    width, height = reference_size(package_asset(args.observation, "reference.png").read_bytes())
    return GenerationOptions(width=getattr(args, "width", width), height=getattr(args, "height", height), time=getattr(args, "time", 0.0))


def main() -> int:
    """输出结构化规划结果索引, 诊断写到 stderr.

    Returns:
        完整规划返回 0, 部分完成或失败返回 1, 输入错误返回 2.
    """
    args = _parser().parse_args()
    try:
        options = load_planning_options(getattr(args, "config", None))
        overrides = {item.name: getattr(args, item.name) for item in fields(PlanningOptions) if hasattr(args, item.name)}
        options = apply_planning_overrides(options, overrides)
        with analysis_console(sys.stderr, args.log_level):
            outcome = run_planning(args.observation, args.request, options=options, generation_options=_generation_options(args))
    except (OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"Error: {error}\n")
        return 2
    sys.stderr.write(f"Run: {outcome.run_dir}\nStatus: {outcome.status}\n")
    sys.stdout.write(
        json.dumps(
            {
                "status": outcome.status,
                "run_dir": str(outcome.run_dir),
                "report_dir": str(outcome.report_dir) if outcome.report_dir else None,
                "error": outcome.error,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return 0 if outcome.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
