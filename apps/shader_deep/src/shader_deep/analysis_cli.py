"""兼容旧导入路径; 实现位于 cli.analysis."""

from shader_deep.cli.analysis import main as main

if __name__ == "__main__":
    raise SystemExit(main())
