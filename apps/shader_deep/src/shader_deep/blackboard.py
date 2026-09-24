"""兼容旧导入路径; 实现位于 domain.blackboard."""

from shader_deep.domain.blackboard import (
    _check_result_candidates as _check_result_candidates,
    _check_task_inputs as _check_task_inputs,
    _require_new as _require_new,
    add_candidate as add_candidate,
    add_result as add_result,
    add_target as add_target,
    add_task as add_task,
    new_blackboard as new_blackboard,
    read_task as read_task,
)
