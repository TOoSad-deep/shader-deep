"""启动 Shader Deep 本地可视化界面."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shader_deep.web.server import LocalServer


def main() -> int:
    """运行本机页面, Ctrl+C 关闭服务并停止仍在运行的任务."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口 (默认 8765)")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/web"), help="页面任务的制品目录")
    args = parser.parse_args()
    try:
        with LocalServer(args.port, args.output_dir) as server:
            sys.stdout.write(f"Shader Deep: {server.origin}\n按 Ctrl+C 停止服务. 模型配置沿用启动环境.\n")
            sys.stdout.flush()
            server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        parser.exit(1, f"无法启动本地界面: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
