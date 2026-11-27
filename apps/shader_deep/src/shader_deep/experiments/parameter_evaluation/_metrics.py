"""在原始坐标计算误差与局部 SSIM, 不缩放、配准或寻找候选 ROI."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageChops, ImageStat

SSIM_WINDOW = 7


def decode_png(data: bytes) -> Image.Image:
    """解码 PNG 并在白底合成透明像素, 返回独立 RGB 图像."""
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG":
                msg = "Evaluation requires PNG bytes"
                raise ValueError(msg)
            with (
                source.convert("RGBA") as rgba,
                Image.new("RGBA", source.size, "white") as background,
                Image.alpha_composite(background, rgba) as composed,
            ):
                return composed.convert("RGB")
    except OSError as exc:
        msg = "Unable to decode PNG bytes"
        raise ValueError(msg) from exc


def region_metrics(reference: Image.Image, candidate: Image.Image, rectangle: tuple[int, int, int, int]) -> dict[str, object]:
    """计算固定矩形的编码 RGB 误差和 7x7 有效窗口亮度 SSIM."""
    with reference.crop(rectangle) as left, candidate.crop(rectangle) as right:
        with ImageChops.difference(left, right) as difference:
            stats = ImageStat.Stat(difference)
            count = left.width * left.height * 3
            mae = sum(stats.sum) / count / 255
            rmse = math.sqrt(sum(stats.sum2) / count) / 255
        return {"rectangle": list(rectangle), "pixels": left.width * left.height, "mae": mae, "rmse": rmse, "ssim_luma": _ssim(left, right)}


def _luminance(image: Image.Image) -> list[float]:
    # 固定编码亮度近似, 不将 Pillow 的 8 位 L 量化引入协方差.
    pixels = image.tobytes()
    return [0.299 * red + 0.587 * green + 0.114 * blue for red, green, blue in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)]


def background_metrics(reference: Image.Image, candidate: Image.Image, foreground: tuple[int, int, int, int]) -> dict[str, object]:
    """计算固定前景矩形的补集误差, 不为非矩形掩码报告 SSIM."""
    left, top, right, bottom = foreground
    width, height = reference.size
    rectangles = ((0, 0, width, top), (0, bottom, width, height), (0, top, left, bottom), (right, top, width, bottom))
    absolute = squared = 0.0
    pixels = 0
    for rectangle in rectangles:
        with reference.crop(rectangle) as first, candidate.crop(rectangle) as second, ImageChops.difference(first, second) as difference:
            stats = ImageStat.Stat(difference)
            absolute += sum(stats.sum)
            squared += sum(stats.sum2)
            pixels += first.width * first.height
    return {
        "rectangles": [list(rectangle) for rectangle in rectangles],
        "pixels": pixels,
        "mae": absolute / (pixels * 3 * 255),
        "rmse": math.sqrt(squared / (pixels * 3)) / 255,
        "ssim_luma": None,
    }


def _integral(values: list[float], width: int, height: int) -> list[float]:
    result = [0.0] * ((width + 1) * (height + 1))
    for y in range(height):
        row_sum = 0.0
        for x in range(width):
            row_sum += values[y * width + x]
            index = (y + 1) * (width + 1) + x + 1
            result[index] = row_sum + result[index - width - 1]
    return result


def _window_sum(integral: list[float], stride: int, x: int, y: int) -> float:
    top, bottom = y * stride + x, (y + 7) * stride + x
    return integral[bottom + 7] - integral[top + 7] - integral[bottom] + integral[top]


def _local_ssim(moments: list[float]) -> float:
    mean_x, mean_y, square_x, square_y, product = (value / 49 for value in moments)
    # 与 7x7 uniform/sample-covariance 变体一致, 裁掉半窗口边界.
    variance_x = max(0.0, (square_x - mean_x**2) * 49 / 48)
    variance_y = max(0.0, (square_y - mean_y**2) * 49 / 48)
    covariance = (product - mean_x * mean_y) * 49 / 48
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * mean_x * mean_y + c1) * (2 * covariance + c2)) / ((mean_x**2 + mean_y**2 + c1) * (variance_x + variance_y + c2))


def _ssim(reference: Image.Image, candidate: Image.Image) -> float | None:
    width, height = reference.size
    if min(width, height) < SSIM_WINDOW:
        return None
    left, right = _luminance(reference), _luminance(candidate)
    moments = [left, right, [value * value for value in left], [value * value for value in right], [x * y for x, y in zip(left, right, strict=True)]]
    integrals = [_integral(values, width, height) for values in moments]
    scores = (_local_ssim([_window_sum(values, width + 1, x, y) for values in integrals]) for y in range(height - 6) for x in range(width - 6))
    return math.fsum(scores) / ((width - 6) * (height - 6))
