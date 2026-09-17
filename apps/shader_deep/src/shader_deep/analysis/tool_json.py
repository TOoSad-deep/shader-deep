"""仅修复可通过工具结构校验的有限语法错误, 不猜测缺失内容."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from langchain.tools import BaseTool

MAX_SURPLUS_DELIMITERS = 2


def _reject_constant(value: str) -> object:
    """拒绝 JSON 标准未定义的 NaN 和 Infinity."""
    msg = f"Nonstandard JSON constant: {value}"
    raise ValueError(msg)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """重复键具有覆盖歧义, 不接受解析器静默选择最后一个值."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"Duplicate JSON key: {key[:80]}"
            raise ValueError(msg)
        result[key] = value
    return result


def decode_object(raw: str) -> dict[str, object]:
    """严格解码一个完整工具参数对象, 不使用底层流式解析器补齐的结果.

    Args:
        raw: 模型服务返回的完整原始参数字符串.

    Returns:
        未补全或删改字段的 JSON 对象.

    Raises:
        ValueError: 参数不是严格、完整且无重复键的 JSON.
        TypeError: 参数的顶层不是 JSON 对象.
    """
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        msg = "Tool arguments must be one JSON object"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def recover_object(raw: str, tool: BaseTool) -> dict[str, object] | None:
    """恢复尾部仅多出闭合符号的完整 JSON 对象, 并校验工具参数结构.

    Args:
        raw: 模型正常结束后返回的原始工具参数字符串.
        tool: 当前角色已获准使用的工具; 恢复结果必须通过完整入参校验.

    Returns:
        内容保持原样的解码对象; 含歧义、不完整或不符合规则时返回 `None`.
    """
    try:
        content = raw.lstrip()
        value, end = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant).raw_decode(content)
        trailing = content[end:].strip()
        # 只接受完整对象之后多出 1~2 个闭合符号的情况; 不补字段、不拼接第二个对象,
        # 也不从自然语言或 Markdown 中猜测参数, 避免把不完整意图转换成可执行调用.
        if not isinstance(value, dict) or not trailing or len(trailing) > MAX_SURPLUS_DELIMITERS or set(trailing) - set("}]"):
            return None
        TypeAdapter(tool.get_input_schema()).validate_python(value)
        return cast("dict[str, object]", value)
    except (ValueError, RecursionError):
        return None


def syntax_feedback(raw: str) -> str:
    """返回带错误位置和有限长度原文片段的 JSON 语法诊断.

    Args:
        raw: 无法使用的模型工具参数字符串.

    Returns:
        简短的错误诊断, 不回显整份报告.
    """
    try:
        decode_object(raw)
    except json.JSONDecodeError as exc:
        # json.dumps 将错误附近的片段显式标成字符串, 限制回显长度并转义控制字符.
        excerpt = json.dumps(raw[max(0, exc.pos - 60) : exc.pos + 60], ensure_ascii=False)
        return (
            f"JSON syntax error: {exc.msg}; line {exc.lineno}, column {exc.colno}. "
            f"Quoted data near error: {excerpt}. Escape quotes inside strings and submit exactly one JSON object."
        )
    except RecursionError:
        return "JSON syntax error: excessive nesting; submit a smaller object."
    except (TypeError, ValueError) as exc:
        return f"JSON syntax error: {str(exc)[:160]}; submit one unambiguous JSON object."
    return "JSON syntax error: arguments could not be used; submit one complete object matching the tool schema."
