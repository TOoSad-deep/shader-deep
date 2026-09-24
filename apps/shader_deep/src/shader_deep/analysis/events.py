"""兼容旧导入路径; 实现位于 runtime.events."""

from shader_deep.infrastructure.tracing import EventLog as EventLog
from shader_deep.runtime.events import EventCallback as EventCallback, NoProgressGuard as NoProgressGuard, failure_signature as failure_signature
