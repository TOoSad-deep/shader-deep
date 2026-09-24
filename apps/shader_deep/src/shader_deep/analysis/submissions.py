"""兼容旧导入路径; 实现位于 runtime.submissions.handler."""

from shader_deep.runtime.submissions.handler import (
    RepairArguments as RepairArguments,
    SubmissionChange as SubmissionChange,
    SubmissionHandler as SubmissionHandler,
    SubmissionReply as SubmissionReply,
    _apply_change as _apply_change,
    _apply_changes as _apply_changes,
    _child as _child,
    _schema_issues as _schema_issues,
    _segments as _segments,
    resolve_pointer as resolve_pointer,
)
