"""兼容旧导入路径; 实现位于 runtime.budgets."""

from shader_deep.runtime.budgets import (
    RequestBudgetError as RequestBudgetError,
    check_request as check_request,
    estimated_tokens as estimated_tokens,
    remaining_material_chars as remaining_material_chars,
)
