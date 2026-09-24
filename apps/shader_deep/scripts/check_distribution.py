"""在临时解包目录检查本仓库构建的 wheel, 不修改当前安装环境."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

IMPORT_CHECK = """
import sys, importlib, pkgutil
from pathlib import Path
import shader_deep
if not Path(shader_deep.__file__).is_relative_to(Path.cwd()):
    raise RuntimeError('Imported the editable checkout instead of the wheel')
from shader_deep.domain.library.store import LibraryStore
if any(name.startswith(('langchain', 'deepagents', 'shader_deep.infrastructure')) for name in sys.modules):
    raise RuntimeError('Domain import crossed an execution or IO boundary')
from shader_deep import api
if any(name.startswith('shader_deep.compatibility') for name in sys.modules):
    raise RuntimeError('Current API loaded a historical execution path')
from shader_deep.workflows.configuration import DEFAULT_CONFIG, load_analysis_options
if not DEFAULT_CONFIG.is_file() or load_analysis_options().max_integration_calls < 1:
    raise RuntimeError('Bundled configuration is unavailable')
for item in pkgutil.walk_packages(shader_deep.__path__, shader_deep.__name__ + '.'):
    if not item.name.endswith('.__main__'):
        importlib.import_module(item.name)
if Path('runs').exists():
    raise RuntimeError('Import unexpectedly created a run')
"""
CLI_MODULES = (
    "shader_deep.cli",
    "shader_deep.cli.analysis",
    "shader_deep.cli.generation",
    "shader_deep.cli.replay",
    "shader_deep.analysis_cli",
    "shader_deep.analysis.replay",
)


def _run(arguments: list[str], root: Path) -> str:
    environment = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(  # noqa: S603  # 只运行当前解释器及固定检查代码, 不经 shell 执行输入.
        [sys.executable, "-W", "error", *arguments], cwd=root, env=environment, check=True, text=True, capture_output=True
    )
    return result.stdout


def check_wheel(wheel: Path) -> None:
    """验证本地构建包的资源、导入隔离与兼容 CLI.

    Args:
        wheel: 本仓库构建的受信 wheel; 导入检查会执行其中的 Python 代码.
    """
    with tempfile.TemporaryDirectory(prefix="shader-installed-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(wheel) as archive:
            required = {"shader_deep/resources/analysis.yaml", "shader_deep/rendering/webgl2.js"}
            if not required <= set(archive.namelist()):
                msg = "Wheel is missing default configuration or WebGL2 resources"
                raise ValueError(msg)
            archive.extractall(root)
        _run(["-c", IMPORT_CHECK], root)
        for module in CLI_MODULES:
            if "usage:" not in _run(["-m", module, "--help"], root):
                msg = f"CLI did not display help: {module}"
                raise ValueError(msg)


def main() -> int:
    """检查一个明确选择的 wheel, 导入失败时保留子进程输出."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    try:
        check_wheel(parser.parse_args().wheel.resolve())
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stdout + exc.stderr)
        return 1
    sys.stdout.write("Wheel checks: OK (resources, import isolation, compatibility imports, 6 CLI modules)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
