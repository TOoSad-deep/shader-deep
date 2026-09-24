"""兼容旧导入路径; 实现位于 runtime.submissions.parsing."""

from shader_deep.runtime.submissions.parsing import (
    MAX_SURPLUS_DELIMITERS as MAX_SURPLUS_DELIMITERS,
    _reject_constant as _reject_constant,
    _unique_object as _unique_object,
    decode_object as decode_object,
    recover_object as recover_object,
    syntax_feedback as syntax_feedback,
)
