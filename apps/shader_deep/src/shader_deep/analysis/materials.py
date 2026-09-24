"""兼容旧导入路径; 实现位于 domain.library.materials."""

from shader_deep.domain.library.materials import (
    ComparisonGroup as ComparisonGroup,
    ComparisonManager as ComparisonManager,
    Document as Document,
    MaterialStore as MaterialStore,
    _canonical as _canonical,
    _canonical_choices as _canonical_choices,
    _compact_work_index as _compact_work_index,
    _comparison_error as _comparison_error,
    _dump as _dump,
    _group as _group,
    _hash as _hash,
    _judgment_fingerprint as _judgment_fingerprint,
    _unique as _unique,
    _work_detail as _work_detail,
    _work_index as _work_index,
)
from shader_deep.domain.library.queue import MaterialPackage as MaterialPackage, MaterialQueue as MaterialQueue
