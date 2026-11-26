"""按 PNG 原始 16 位样本裁剪, 保留透明键和显式 alpha.

实现依据 W3C PNG 第三版第 8/9 节的 Adam7 与字节级逆过滤规则.
https://www.w3.org/TR/png-3/
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from PIL import Image

from shader_deep.imaging.png_chunks import IHDR_LENGTH, PNG_SIGNATURE, png_chunks

MAX_PIXELS = 16_777_216
MAX_FILE_BYTES = 32 * 1024 * 1024
BIT_DEPTH = 16
FILTER_SUB = 1
FILTER_UP = 2
FILTER_AVERAGE = 3
FILTER_PAETH = 4
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


@dataclass(frozen=True, kw_only=True)
class Png16Samples:
    """固定色型的行优先、大端 16 位原始样本和透明键."""

    width: int
    height: int
    color_type: int
    samples: bytes
    transparency: bytes | None


def _fail(message: str) -> ValueError:
    return ValueError(f"16 位评审 PNG 无效: {message}")


def _chunks(content: bytes) -> tuple[tuple[bytes, bytes], ...]:
    if len(content) > MAX_FILE_BYTES:
        msg = "文件超过容量限制"
        raise _fail(msg)
    return tuple((chunk[4:8], chunk[8:-4]) for chunk in png_chunks(content))


def _header(chunks: tuple[tuple[bytes, bytes], ...]) -> tuple[int, int, int, int]:
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != IHDR_LENGTH:
        msg = "缺少合法 IHDR"
        raise _fail(msg)
    width, height, depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if not 0 < width * height <= MAX_PIXELS or depth != BIT_DEPTH or color_type not in CHANNELS:
        msg = "尺寸、位深或色型不支持"
        raise _fail(msg)
    if compression or filtering or interlace not in (0, 1):
        msg = "压缩、过滤或交错方式不支持"
        raise _fail(msg)
    if sum(kind == b"IHDR" for kind, _ in chunks) != 1:
        msg = "重复 IHDR"
        raise _fail(msg)
    if any(kind == b"acTL" for kind, _ in chunks):
        msg = "只支持静态 PNG"
        raise _fail(msg)
    return width, height, color_type, interlace


def _passes(width: int, height: int, interlace: int) -> tuple[tuple[int, int, int, int, int, int], ...]:
    result = []
    for left, top, dx, dy in ADAM7 if interlace else ((0, 0, 1, 1),):
        columns = max(0, (width - left + dx - 1) // dx)
        rows = max(0, (height - top + dy - 1) // dy)
        if columns and rows:
            result.append((left, top, dx, dy, columns, rows))
    return tuple(result)


def _inflate(compressed: bytes, expected: int) -> bytes:
    decoder = zlib.decompressobj()
    try:
        data = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        msg = "IDAT 压缩流损坏"
        raise _fail(msg) from error
    if len(data) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        msg = "IDAT 解压长度或流终态不符合 IHDR"
        raise _fail(msg)
    return data


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    distances = (abs(prediction - left), abs(prediction - above), abs(prediction - upper_left))
    if distances[0] <= distances[1] and distances[0] <= distances[2]:
        return left
    return above if distances[1] <= distances[2] else upper_left


def _predictor(method: int, left: int, above: int, upper_left: int) -> int:
    if method == FILTER_SUB:
        return left
    if method == FILTER_AVERAGE:
        return (left + above) // 2
    return _paeth(left, above, upper_left)


def _unfilter(filtered: bytes, previous: bytes, bpp: int, method: int) -> bytes:
    if method == 0:
        return filtered
    if method == FILTER_UP:
        return bytes((value + previous[index]) & 255 for index, value in enumerate(filtered))
    row = bytearray(filtered)
    for index, value in enumerate(row):
        left = row[index - bpp] if index >= bpp else 0
        above = previous[index]
        upper_left = previous[index - bpp] if index >= bpp else 0
        predictor = _predictor(method, left, above, upper_left)
        row[index] = (value + predictor) & 255
    return bytes(row)


def _decode_samples(data: bytes, width: int, height: int, bpp: int, interlace: int, box: tuple[int, int, int, int]) -> bytes:
    x0, y0, x1, y1 = box
    stride = x1 - x0
    samples = bytearray(stride * (y1 - y0) * bpp)
    offset = 0
    for left, top, dx, dy, columns, rows in _passes(width, height, interlace):
        first = max(0, (x0 - left + dx - 1) // dx)
        stop = min(columns, max(0, (x1 - left + dx - 1) // dx))
        previous = bytes(stop * bpp)
        for y in range(top, top + rows * dy, dy):
            method = data[offset]
            if method > FILTER_PAETH:
                msg = "扫描行过滤类型错误"
                raise _fail(msg)
            offset += 1
            # 过滤预测只依赖本行左侧及上一行, 裁剪右方或下方的样本无需重建.
            if first < stop and y < y1:
                previous = _unfilter(data[offset : offset + stop * bpp], previous, bpp, method)
                if y >= y0:
                    pixel = (y - y0) * stride + left + first * dx - x0
                    _scatter_row(samples, previous[first * bpp :], pixel, dx, bpp)
            offset += columns * bpp
    return bytes(samples)


def _scatter_row(samples: bytearray, row: bytes, pixel: int, dx: int, bpp: int) -> None:
    if dx == 1:
        samples[pixel * bpp : pixel * bpp + len(row)] = row
        return
    for column in range(len(row) // bpp):
        destination = (pixel + column * dx) * bpp
        samples[destination : destination + bpp] = row[column * bpp : (column + 1) * bpp]


def decode_png16(content: bytes) -> Png16Samples:
    """有限解压并重建全部 16 位通道, 支持非交错与 Adam7.

    Args:
        content: 静态 PNG 原始字节.

    Returns:
        保留原始样本精度和透明键的不可变像素记录.

    Raises:
        ValueError: 输入结构、容量、过滤或压缩数据无效.
    """
    return _decode_region(content, None)


def display_png16(content: bytes, box: tuple[int, int, int, int] | None = None) -> Image.Image:
    """将固定图像的原始 16 位样本投影到评审与测量使用的 8 位 RGBA.

    Args:
        content: 16 位静态 PNG 字节.
        box: 可选的左闭右开裁剪框, 采用原图像素坐标.

    Returns:
        各通道取高 8 位的 RGBA 图像, 由调用方关闭.
    """
    decoded = _decode_region(content, box)
    mode = {0: "L", 2: "RGB", 4: "LA", 6: "RGBA"}[decoded.color_type]
    size = (decoded.width, decoded.height)
    with Image.frombytes(mode, size, decoded.samples[::2]) as channels:
        rgba = channels.convert("RGBA")
    key = decoded.transparency
    if key is not None:
        # 完整色键区分高字节相同、低字节不同的透明及不透明像素.
        data = decoded.samples
        stride = len(key)
        alpha = bytes(0 if data[index : index + stride] == key else 255 for index in range(0, len(data), stride))
        with Image.frombytes("L", size, alpha) as mask:
            rgba.putalpha(mask)
    return rgba


def _decode_region(content: bytes, box: tuple[int, int, int, int] | None) -> Png16Samples:
    chunks = _chunks(content)
    width, height, color_type, interlace = _header(chunks)
    region = box if box is not None else (0, 0, width, height)
    left, top, right, bottom = region
    if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
        msg = "裁剪区域越界"
        raise _fail(msg)
    bpp = CHANNELS[color_type] * 2
    expected = sum(rows * (1 + columns * bpp) for _, _, _, _, columns, rows in _passes(width, height, interlace))
    data = _inflate(b"".join(data for kind, data in chunks if kind == b"IDAT"), expected)
    transparent = [data for kind, data in chunks if kind == b"tRNS"]
    if len(transparent) > 1 or (transparent and (color_type not in (0, 2) or len(transparent[0]) != bpp)):
        msg = "tRNS 透明键与色型不一致"
        raise _fail(msg)
    return Png16Samples(
        width=right - left,
        height=bottom - top,
        color_type=color_type,
        samples=_decode_samples(data, width, height, bpp, interlace, region),
        transparency=transparent[0] if transparent else None,
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def crop_png16(content: bytes, box: tuple[int, int, int, int]) -> bytes:
    """裁剪原始样本并输出同位深、同色型的非交错 PNG.

    Args:
        content: 已选定的静态 16 位 PNG.
        box: 原图中的左闭右开像素矩形.

    Returns:
        保留显式 alpha 与透明键的裁剪 PNG. 色彩元数据由上层捕获规则保留.

    Raises:
        ValueError: 输入无效或区域越界.
    """
    image = _decode_region(content, box)
    stride = image.width * CHANNELS[image.color_type] * 2
    rows = (b"\0" + image.samples[start : start + stride] for start in range(0, len(image.samples), stride))
    header = struct.pack(">IIBBBBB", image.width, image.height, BIT_DEPTH, image.color_type, 0, 0, 0)
    transparency = _chunk(b"tRNS", image.transparency) if image.transparency is not None else b""
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + transparency + _chunk(b"IDAT", zlib.compress(b"".join(rows))) + _chunk(b"IEND", b"")
