"""兼容旧导入路径; 实现位于 compatibility.ondemand.session."""

from shader_deep.compatibility.ondemand.session import (
    MAX_CONTEXT_PAGE_SIZE as MAX_CONTEXT_PAGE_SIZE,
    PROTOCOL as PROTOCOL,
    ExplorationSession as ExplorationSession,
    FutureResult as FutureResult,
    RepairContextError as RepairContextError,
    StaleMaterialError as StaleMaterialError,
    _json as _json,
)
