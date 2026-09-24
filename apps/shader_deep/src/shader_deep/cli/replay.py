"""从冻结探索输入启动有界整合回放."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shader_deep.workflows.configuration import apply_analysis_overrides, load_analysis_options
from shader_deep.workflows.replay import run_replay


def main() -> int:
    """启动显式有界回放, 标准输出只包含简短运行位置与状态."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--max-main-calls", type=int, default=8)
    parser.add_argument("--integration-max-output-tokens", type=int)
    args = parser.parse_args()
    if args.max_main_calls < 1:
        parser.error("Replay max-main-calls must be positive")
    try:
        overrides = {"output_dir": args.output_dir, "max_main_calls": args.max_main_calls}
        if args.integration_max_output_tokens is not None:
            overrides["integration_max_output_tokens"] = args.integration_max_output_tokens
        outcome = run_replay(args.source, options=apply_analysis_overrides(load_analysis_options(args.config), overrides))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    sys.stdout.write(json.dumps({"status": outcome.stop_reason, "run_dir": str(outcome.run_dir)}, ensure_ascii=False) + "\n")
    return 0 if outcome.stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
