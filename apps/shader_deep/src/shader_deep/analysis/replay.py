"""兼容旧导入路径; 实现位于 workflows.replay."""

from shader_deep.cli.replay import main as main
from shader_deep.workflows.replay import (
    ReplayInput as ReplayInput,
    _mapping as _mapping,
    _prepare_replay as _prepare_replay,
    _restore_issues as _restore_issues,
    _restore_results as _restore_results,
    _restore_tasks as _restore_tasks,
    _restore_workers as _restore_workers,
    _result as _result,
    _root_state as _root_state,
    _Session as _Session,
    _task as _task,
    _write_manifest as _write_manifest,
    load_replay_input as load_replay_input,
    prepare_replay as prepare_replay,
    run_replay as run_replay,
)

if __name__ == "__main__":
    raise SystemExit(main())
