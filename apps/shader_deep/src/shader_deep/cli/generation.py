"""根据 PNG 或明确选择的五库报告方案生成兼容 ShaderToy 的 GLSL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.api import run_generation_from_report, run_shader

if TYPE_CHECKING:
    from shader_deep.agents.generation.contracts import GenerationOutcome

PNG_POSITIONAL_COUNT = 2


def _input_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    if args.report is not None:
        if len(args.inputs) != 1 or args.sketch is None or args.background is None:
            parser.error("report mode requires one PROMPT plus --report, --sketch and --background; do not also pass a PNG")
        args.png, args.prompt = None, args.inputs[0]
    else:
        if len(args.inputs) != PNG_POSITIONAL_COUNT or args.sketch is not None or args.background is not None or args.alternative is not None:
            parser.error("PNG mode requires PNG PROMPT; report selection flags require --report")
        args.png, args.prompt = Path(args.inputs[0]), args.inputs[1]
        args.width = 512 if args.width is None else args.width
        args.height = 512 if args.height is None else args.height
    return args


def parse_args() -> argparse.Namespace:
    """解析 PNG 或报告输入模式及执行要求.

    Returns:
        含显式输入模式、提示词及渲染覆盖值的命令行参数.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    # argparse 做字符串到 Path/int/float 的转换; 正数和有限值约束由配置对象检查.
    parser.add_argument("inputs", nargs="+", metavar="INPUT", help="PNG PROMPT, or only PROMPT with --report")
    parser.add_argument("--report", type=Path, help="Path to a complete five-library report")
    parser.add_argument("--sketch", help="Explicit sketch ID in the report")
    parser.add_argument("--background", help="Required background behavior for report generation")
    parser.add_argument("--alternative", type=int, help="Optional zero-based local alternative index")
    parser.add_argument("--width", type=int, help="Render width (PNG default: 512; report default: reference width)")
    parser.add_argument("--height", type=int, help="Render height (PNG default: 512; report default: reference height)")
    parser.add_argument("--time", type=float, default=0.0, help="Fixed iTime value in seconds")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum render attempts, including failures")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for a unique run folder (default: runs)")
    return _input_mode(parser, parser.parse_intermixed_args())


def _run(args: argparse.Namespace) -> GenerationOutcome:
    if args.report is not None:
        return run_generation_from_report(
            args.report,
            args.sketch,
            args.prompt,
            background=args.background,
            alternative=args.alternative,
            width=args.width,
            height=args.height,
            time=args.time,
            max_attempts=args.max_attempts,
            output_dir=args.output_dir,
        )
    options = GenerationOptions(
        width=args.width,
        height=args.height,
        time=args.time,
        max_attempts=args.max_attempts,
        output_dir=args.output_dir,
    )
    return run_shader(args.png, args.prompt, options=options)


def main() -> int:
    """运行命令行程序, 将 GLSL 写入标准输出.

    Returns:
        完成时返回 0, 受阻或达到预算时返回 1, 输入或文件错误时返回 2.
    """
    args = parse_args()
    try:
        outcome = _run(args)
        # 诊断信息写 stderr, 让 stdout 可以直接重定向成 .glsl 文件而不混入日志.
        sys.stderr.write(f"Run: {outcome.run_dir}\n")
        if outcome.selected_candidate is None:
            sys.stderr.write(f"Generation incomplete: {outcome.stop_reason}\n")
            return 1
        # 交付选定候选的落盘代码, 保持最终输出与实际渲染的版本一致.
        sys.stdout.write(f"{Path(outcome.selected_candidate.code_path).read_text(encoding='utf-8')}\n")
        sys.stderr.write(f"Preview: {outcome.selected_candidate.preview_path}\n")
    except (OSError, TypeError, ValueError) as exc:
        # 输入和本地文件错误统一返回 2; 未列出的异常仍向上抛出, 保留诊断信息.
        sys.stderr.write(f"Error: {exc}\n")
        for note in getattr(exc, "__notes__", ()):
            sys.stderr.write(f"{note}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
