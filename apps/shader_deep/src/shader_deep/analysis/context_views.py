"""兼容旧导入路径; 实现位于 domain.library.context_views."""

from shader_deep.domain.library.context_views import (
    MAX_PAGE_ENTRIES as MAX_PAGE_ENTRIES,
    _compact_context_row as _compact_context_row,
    bounded_context_page as bounded_context_page,
)
