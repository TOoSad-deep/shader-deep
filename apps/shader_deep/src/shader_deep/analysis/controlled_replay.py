"""兼容旧导入路径; 实现位于 experiments.controlled_replay."""

from shader_deep.experiments.controlled_replay import (
    _CANDIDATES as _CANDIDATES,
    _F1 as _F1,
    _F2 as _F2,
    _F3 as _F3,
    _F4 as _F4,
    _extend_manifest as _extend_manifest,
    _write_summary as _write_summary,
    fixed_works as fixed_works,
    main as main,
    run_controlled_replay as run_controlled_replay,
)

if __name__ == "__main__":
    raise SystemExit(main())
