"""显式文本引用解析与一次重写, 不推断自然语言含义."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping

MARKER = re.compile(r"\[\[([^\[\]\r\n]+)\]\]")
BARE_ID = re.compile(r"(?<![A-Za-z0-9_])(?:E|F|R|M|S)\d+(?![A-Za-z0-9_])")


class LibraryValidationError(ValueError):
    """提供具体字段、原文及修复原因."""

    def __init__(self, field: str, text: str, reason: str) -> None:
        """保留具体错误上下文供局部提交修复."""
        self.field = field
        self.text = text
        self.reason = reason
        super().__init__(f"{field}: {reason}; text={text!r}")


def fail(field: str, text: object, reason: str) -> NoReturn:
    """抛出可反馈至当前提交者的校验问题."""
    raise LibraryValidationError(field, str(text), reason)


def parse_references(text: str, index: Mapping[str, str], *, field: str) -> tuple[str, ...]:
    """校验标记及疑似裸 ID, 返回精确匹配的引用.

    Args:
        text: 原始说明.
        index: 当前来源明确可见的对象索引.
        field: 修复问题中的字段位置.

    Returns:
        按出现顺序排列的对象引用.

    Raises:
        LibraryValidationError: 引用不存在、格式错误或疑似未标记.
    """
    refs = tuple(match.group(1) for match in MARKER.finditer(text))
    remainder = MARKER.sub("", text)
    if "[[" in remainder or "]]" in remainder:
        fail(field, text, "标记不能为空、嵌套或跨行")
    for ref in refs:
        if ref not in index:
            fail(field, text, f"标记引用 {ref!r} 不存在; 必须精确匹配")
    suspected = BARE_ID.search(remainder)
    if suspected:
        fail(field, text, f"疑似未标记对象引用 {suspected.group()!r}; 使用 [[ID]]")
    for ref in index:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", remainder):
            fail(field, text, f"疑似未标记对象引用 {ref!r}; 使用 [[ID]]")
    return refs


def resolve_mapping(index: Mapping[str, str], mapping: Mapping[str, str]) -> dict[str, str]:
    """解析合并链后生成直接映射; 禁止循环及跨类型.

    Args:
        index: 映射前后对象的类型索引.
        mapping: 尚未归一化的映射.

    Returns:
        每个旧 ID 直接指向最终对象的映射.

    Raises:
        LibraryValidationError: 映射无目标、循环或跨类型.
    """
    result: dict[str, str] = {}
    for source in mapping:
        target, visited = source, set()
        while target in mapping and mapping[target] != target:
            if target in visited:
                fail("mapping", source, "映射循环")
            visited.add(target)
            target = mapping[target]
        if source not in index or target not in index:
            fail("mapping", source, "映射源或目标不存在")
        if index[source] != index[target]:
            fail("mapping", source, "映射不能改变对象类型")
        result[source] = target
    return result


def rewrite_text(text: str, mapping: Mapping[str, str]) -> str:
    """依据原文标记一次重写, 保持非引用文字原样.

    Args:
        text: 已校验的原始正文.
        mapping: 已解析的直接映射.

    Returns:
        仅显式对象引用被更新的正文.
    """
    return MARKER.sub(lambda match: f"[[{mapping.get(match.group(1), match.group(1))}]]", text)
