"""旧引用目录入口的兼容转发."""

from shader_deep.domain.references import (
    SourceCatalogEntry as SourceCatalogEntry,
    _entry as _entry,
    report_catalog as report_catalog,
    unlinked_interpretations as unlinked_interpretations,
)
