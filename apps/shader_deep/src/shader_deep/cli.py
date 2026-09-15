"""根据 PNG 参考图和文字提示生成兼容 ShaderToy 的 GLSL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shader_deep.agents.generation import run_shader
from shader_deep.config import GenerationOptions


def parse_args() -> argparse.Namespace:
    """解析 PNG 路径和用户提示词.

    Returns:
        包含本地图片路径和提示词的命令行参数.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    # argparse 做字符串到 Path/int/float 的转换; 正数和有限值约束由配置对象检查.
    parser.add_argument("png", type=Path, help="Path to a local PNG reference")
    parser.add_argument("prompt", help="Instructions for generating the shader")
    parser.add_argument("--width", type=int, default=512, help="Render width in pixels")
    parser.add_argument("--height", type=int, default=512, help="Render height in pixels")
    parser.add_argument("--time", type=float, default=0.0, help="Fixed iTime value in seconds")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum render attempts, including failures")
    parser.add_argument("--output-dir", type=Path, help="Parent directory for a unique run folder (default: runs)")
    return parser.parse_args()


def main() -> int:
    """运行命令行程序, 将 GLSL 写入标准输出.

    Returns:
        完成时返回 0, 达到预算未完成时返回 1, 输入或文件错误时返回 2.
    """
    args = parse_args()
    try:
        options = GenerationOptions(width=args.width, height=args.height, time=args.time, max_attempts=args.max_attempts, output_dir=args.output_dir)
        outcome = run_shader(args.png, args.prompt, options=options)
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
        return 2
    return 0
