"""静态 PNG 的块边界、顺序及 CRC 校验."""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IHDR_LENGTH = 13
CHUNK_OVERHEAD = 12
INDEXED_COLOR = 3
PALETTE_ENTRY_BYTES = 3
MAX_PALETTE_ENTRIES = 256


def _raw_chunks(content: bytes) -> Iterator[bytes]:
    if not content.startswith(PNG_SIGNATURE):
        msg = "PNG 文件签名错误"
        raise ValueError(msg)
    offset = len(PNG_SIGNATURE)
    while offset < len(content):
        end = offset + CHUNK_OVERHEAD + int.from_bytes(content[offset : offset + 4], "big")
        if end > len(content):
            msg = "PNG 块长度越界"
            raise ValueError(msg)
        chunk = content[offset:end]
        if zlib.crc32(memoryview(chunk)[4:-4]) != int.from_bytes(chunk[-4:], "big"):
            msg = "PNG 块 CRC 不匹配"
            raise ValueError(msg)
        yield chunk
        offset = end


def _validate_palette(chunk: bytes, header: bytes, *, misplaced: bool) -> None:
    entries, remainder = divmod(len(chunk) - CHUNK_OVERHEAD, PALETTE_ENTRY_BYTES)
    limit = 1 << header[16] if header[17] == INDEXED_COLOR else MAX_PALETTE_ENTRIES
    if misplaced or header[17] in (0, 4) or remainder or not 0 < entries <= limit:
        msg = "PNG 调色板内容或位置无效"
        raise ValueError(msg)


def png_chunks(content: bytes) -> Iterator[bytes]:
    """按文件顺序读取完整块, 消费至结尾时核对必需块与终态.

    Args:
        content: 固定 PNG 文件字节.

    Yields:
        含长度、块类型、正文与 CRC 的原始块.

    Raises:
        ValueError: 块损坏、缺失或关键块顺序无效.
    """
    chunks = _raw_chunks(content)
    header = next(chunks, b"")
    if header[4:8] != b"IHDR" or len(header) != IHDR_LENGTH + CHUNK_OVERHEAD:
        msg = "PNG 必须以完整 IHDR 开始"
        raise ValueError(msg)
    yield header
    palette = idat = False
    previous = b"IHDR"
    for chunk in chunks:
        kind = chunk[4:8]
        if kind == b"IHDR":
            msg = "PNG 的 IHDR 重复"
            raise ValueError(msg)
        if kind == b"PLTE":
            _validate_palette(chunk, header, misplaced=palette or idat)
            palette = True
        elif kind == b"IDAT":
            if (idat and previous != b"IDAT") or (header[17] == INDEXED_COLOR and not palette):
                msg = "PNG 图像数据不连续或缺少前置调色板"
                raise ValueError(msg)
            idat = True
        elif kind == b"IEND":
            if not idat or len(chunk) != CHUNK_OVERHEAD or next(chunks, None) is not None:
                msg = "PNG 缺少图像数据或 IEND 终态无效"
                raise ValueError(msg)
            yield chunk
            return
        previous = kind
        yield chunk
    msg = "PNG 缺少完整的 IEND"
    raise ValueError(msg)
