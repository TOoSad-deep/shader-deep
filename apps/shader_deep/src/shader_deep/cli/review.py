"""从固定参考图和候选渲染图运行独立视觉评审."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

from shader_deep.domain.visual_review import ReviewRequest
from shader_deep.infrastructure.analysis_logging import analysis_console
from shader_deep.infrastructure.storage.planning_inputs import parse_json
from shader_deep.workflows.review import ReviewOptions, run_review
from shader_deep.workflows.review.configuration import apply_review_overrides, load_review_options


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, argument_default=argparse.SUPPRESS)
    parser.add_argument("reference", type=Path, help="固定参考 PNG")
    parser.add_argument("rendered", type=Path, help="指定候选的实际渲染 PNG")
    parser.add_argument("--task", required=True, type=Path, help="包含范围、目标和六维检查内容的任务 JSON")
    parser.add_argument("--config", type=Path, help="评审 YAML 路径")
    parser.add_argument("--output-dir", type=Path, help="独立运行目录的父目录")
    parser.add_argument("--max-model-calls", type=int, help="逻辑模型调用预算, 默认 3")
    parser.add_argument("--max-request-retries", type=int, help="传输错误的额外重试次数, 默认 2")
    parser.add_argument("--request-timeout-seconds", type=int, help="连接与读取等传输操作超时")
    parser.add_argument("--max-output-tokens", type=int, help="单次请求输出预算, 默认 8192")
    parser.add_argument("--stream-model-responses", action=argparse.BooleanOptionalAction, help="完整接收后校验流式响应")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="stderr 诊断级别")
    return parser


def main() -> int:
    """输出评审索引, 用退出码表达运行交付结果.

    Returns:
        报告交付为 0, 运行失败或受阻为 1, 启动前输入错误为 2.
    """
    args = _parser().parse_args()
    try:
        request = ReviewRequest.model_validate(parse_json(args.task.read_bytes()))
        options = load_review_options(getattr(args, "config", None))
        overrides = {item.name: getattr(args, item.name) for item in fields(ReviewOptions) if hasattr(args, item.name)}
        options = apply_review_overrides(options, overrides)
    except (OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"Error: {error}\n")
        return 2
    try:
        with analysis_console(sys.stderr, args.log_level):
            outcome = run_review(args.reference, args.rendered, request, options=options)
    except (TypeError, ValueError) as error:
        sys.stderr.write(f"Error: {error}\n")
        return 2
    except OSError as error:
        sys.stderr.write(f"Run error: {error}\n")
        sys.stdout.write(json.dumps({"status": "failed", "verdict": None, "error": str(error)}, ensure_ascii=False) + "\n")
        return 1
    sys.stdout.write(
        json.dumps(
            {
                "status": outcome.status,
                "verdict": outcome.verdict,
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
