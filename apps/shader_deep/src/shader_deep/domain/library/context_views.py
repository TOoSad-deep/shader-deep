"""对未解决初稿问题和失败任务提供有界、版本绑定的发现入口."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from shader_deep.domain.library.queries import _bounded_page, _page_offset

if TYPE_CHECKING:
    from shader_deep.domain.library.store import Document

MAX_PAGE_ENTRIES = 100


def _compact_context_row(row: Document) -> Document:
    """预算不足时只保留完整身份, 不截断后再声称正文完整."""
    keys = ("id", "task_id", "element_id", "status")
    return {**{key: row[key] for key in keys if key in row}, "details_unavailable_due_to_budget": True}


def bounded_context_page(section: str, rows: list[Document], *, cursor: str | None = None, limit: int = 30, max_chars: int = 8000) -> Document:
    """返回按条目数量和回执字符数共同限制的问题页.

    Args:
        section: 初稿问题、失败任务或未覆盖元素的稳定类别.
        rows: 后端保存的该类别完整条目.
        cursor: 绑定类别及完整条目快照的续页位置.
        limit: 单页条目上限, 取值为 1 至 100.
        max_chars: 完整 JSON 回执的字符上限.

    Returns:
        含完整或明确超限索引、总数、剩余数及后续游标的页面.

    Raises:
        ValueError: 参数无效、游标过期或身份本身无法放入页面.
    """
    if not 1 <= limit <= MAX_PAGE_ENTRIES or max_chars <= 0:
        msg = "Context page requires limit between 1 and 100 and positive max_chars"
        raise ValueError(msg)
    encoded = json.dumps([section, rows], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    offset = _page_offset(cursor, fingerprint, len(rows))
    return _bounded_page(
        rows, fingerprint=fingerprint, offset=offset, limit=limit, max_chars=max_chars, compact=_compact_context_row, metadata={"section": section}
    )
