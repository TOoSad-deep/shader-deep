"""静态 PNG 的块校验与显示色彩信息保留."""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IHDR_LENGTH = 13
CHUNK_OVERHEAD = 12
INDEXED_COLOR = 3
PALETTE_ENTRY_BYTES = 3
MAX_PALETTE_ENTRIES = 256
GRAYSCALE_COLOR_TYPES = frozenset({0, 4})
_COLOR_CHUNKS = frozenset({b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"cICP", b"sBIT", b"cLLI", b"mDCV"})


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


def png_color_chunks(content: bytes) -> tuple[bytes, ...]:
    """按原顺序返回影响 PNG 显示颜色的完整数据块.

    Args:
        content: 原始 PNG 字节.

    Returns:
        颜色及 HDR 描述的原始块, 含长度和 CRC.
    """
    return tuple(chunk for chunk in png_chunks(content) if chunk[4:8] in _COLOR_CHUNKS)


def copy_png_color_chunks(source: bytes, output: bytes, *, preserve_significant_bits: bool = True) -> bytes:
    """将原图的颜色块及 CRC 原样移入采用相同颜色空间的 PNG.

    Args:
        source: 提供原始颜色信息的 PNG.
        output: 已编码的目标 PNG, 须兼容原图的灰度或 RGB 色彩描述.
        preserve_significant_bits: 保留与原图样本布局一致的 sBIT; 更换布局时关闭.

    Returns:
        图像数据沿用目标 PNG, 色彩描述沿用原图的文件字节.
    """
    chunks = png_chunks(output)
    header = next(chunks)
    colors = tuple(chunk for chunk in png_color_chunks(source) if preserve_significant_bits or chunk[4:8] != b"sBIT")
    return b"".join((PNG_SIGNATURE, header, *colors, *(chunk for chunk in chunks if chunk[4:8] not in _COLOR_CHUNKS)))


def is_low_depth_gray(content: bytes) -> bool:
    """检查已经验证的 PNG 是否使用 1、2 或 4 位灰度样本.

    Args:
        content: 已核对文件头的 PNG 字节.

    Returns:
        样本和透明键需要展开到 8 位时为真.
    """
    return content[25] == 0 and content[24] in (1, 2, 4)


def expand_low_depth_gray(image: Image.Image, content: bytes) -> Image.Image:
    """将已解码的低位深灰度图与其透明键统一扩至 8 位.

    Args:
        image: 已解码的全图或同一 PNG 的局部视图.
        content: 提供位深与透明键的原始 PNG 字节.

    Returns:
        由调用方关闭的 8 位灰度图.
    """
    # Pillow 已扩展灰度样本, 但 tRNS 键仍须按原位深换算.
    gray = image.convert("L")
    for chunk in png_chunks(content):
        if chunk[4:8] == b"tRNS":
            key = int.from_bytes(chunk[8:-4], "big")
            gray.info["transparency"] = key * 255 // ((1 << content[24]) - 1)
            break
    return gray
