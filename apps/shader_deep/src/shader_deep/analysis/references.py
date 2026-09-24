"""兼容旧导入路径; 实现位于 compatibility.analysis.references."""

from shader_deep.domain.references import (
    SourceCatalogEntry as SourceCatalogEntry,
    _entry as _entry,
    report_catalog as report_catalog,
    unlinked_interpretations as unlinked_interpretations,
)
