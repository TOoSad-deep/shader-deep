"""锁定 Shader 结构, 按固定像素目标优化外置参数."""

from shader_deep.optimization.objective import ProtectedRegion
from shader_deep.optimization.uniforms import (
    DEFAULT_OPTIMIZATION_EVALUATIONS,
    MAX_OPTIMIZATION_EVALUATIONS,
    OptimizationResult,
    optimize_uniforms,
)

__all__ = ["DEFAULT_OPTIMIZATION_EVALUATIONS", "MAX_OPTIMIZATION_EVALUATIONS", "OptimizationResult", "ProtectedRegion", "optimize_uniforms"]
