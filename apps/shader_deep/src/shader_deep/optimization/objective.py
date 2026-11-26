"""固定像素坐标上的颜色、边缘与保护区度量."""

from __future__ import annotations

import io
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from itertools import combinations
from statistics import fmean
from typing import TYPE_CHECKING, Annotated

from PIL import Image, ImageChops, ImageFilter, ImageStat
from pydantic import BaseModel, ConfigDict, Field

from shader_deep.domain.visual_review import PixelRegion  # noqa: TC001  # Pydantic 在运行时使用坐标合同.
from shader_deep.imaging.png16 import BIT_DEPTH, display_png16

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

RegionBox = tuple[int, int, int, int]
_MAX_READ_PIXELS = 4_194_304
_MAX_REGIONS = 16
_EVAL_SIDE = 128
_BYTE_RANGE = 255
_LUMA = (0.2126, 0.7152, 0.0722, 0.0)
_SOBEL = (
    ImageFilter.Kernel((3, 3), (-1, 0, 1, -2, 0, 2, -1, 0, 1), scale=8, offset=128),
    ImageFilter.Kernel((3, 3), (-1, -2, -1, 0, 0, 0, 1, 2, 1), scale=8, offset=128),
)


class ProtectedRegion(BaseModel):
    """以起始候选像素为基准, 限制区域内单像素显示颜色或透明度的最大漂移."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    region: PixelRegion
    tolerance: Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)] = 0.0


def _box(region: PixelRegion) -> RegionBox:
    return region.left, region.top, region.right, region.bottom


def _size(box: RegionBox) -> tuple[int, int]:
    return box[2] - box[0], box[3] - box[1]


@contextmanager
def _display(image: Image.Image) -> Iterator[Image.Image]:
    # 白底合成沿用分析和评审的 RGB 统计口径, alpha 则独立保留用于像素目标与保护.
    with image.convert("RGBA") as rgba, Image.new("RGBA", image.size, "white") as background, rgba.getchannel("A") as alpha:
        background.alpha_composite(rgba)
        background.putalpha(alpha)
        yield background


def _statistics(image: Image.Image) -> tuple[float, ...]:
    red, green, blue, _ = ImageStat.Stat(image).mean
    return red, green, blue, red * _LUMA[0] + green * _LUMA[1] + blue * _LUMA[2]


def _vector_loss(a: tuple[float, ...], b: tuple[float, ...], scale: int) -> float:
    return fmean(abs(x - y) for x, y in zip(a, b, strict=True)) / scale


def _anchor_loss(expected: tuple[tuple[float, ...], ...], candidate: Sequence[Image.Image]) -> float:
    observed = tuple(_statistics(image) for image in candidate)
    absolute = fmean(_vector_loss(a, b, _BYTE_RANGE) for a, b in zip(expected, observed, strict=True))
    pairs = tuple(combinations(range(len(observed)), 2))
    if not pairs:
        return absolute
    relative = fmean(
        _vector_loss(
            tuple(x - y for x, y in zip(expected[a], expected[b], strict=True)),
            tuple(x - y for x, y in zip(observed[a], observed[b], strict=True)),
            2 * _BYTE_RANGE,
        )
        for a, b in pairs
    )
    return (absolute + relative) / 2


def _gradient(image: Image.Image, kernel: ImageFilter.Kernel, stack: ExitStack) -> Image.Image:
    luma = stack.enter_context(image.convert("RGB")).convert("L", _LUMA)
    stack.enter_context(luma)
    edges = stack.enter_context(luma.filter(kernel))
    # Pillow 的卷积保留最外侧一圈原像素, 比较时排除这些未经卷积的像素.
    return stack.enter_context(edges.crop((1, 1, edges.width - 1, edges.height - 1)))


def _thumbnail(image: Image.Image, stack: ExitStack) -> Image.Image:
    rgb = stack.enter_context(image.convert("RGB"))
    alpha = stack.enter_context(image.getchannel("A"))
    # 分别缩放 RGB 和 alpha, 防止 Pillow 缩放 RGBA 时再次按透明度加权白底 RGB.
    rgb.thumbnail((_EVAL_SIDE, _EVAL_SIDE), Image.Resampling.BOX)
    reduced = stack.enter_context(rgb.convert("RGBA"))
    reduced.putalpha(stack.enter_context(alpha.resize(rgb.size, Image.Resampling.BOX)))
    return reduced


@contextmanager
def _roi_features(image: Image.Image) -> Iterator[tuple[Image.Image, ...]]:
    with ExitStack() as stack:
        # 颜色减采样用区域平均, 避免最近邻忽略窄亮边; 保持原始宽高比.
        reduced = _thumbnail(image, stack)
        if min(reduced.size) < 3:  # noqa: PLR2004  # Sobel 固定使用 3x3 邻域.
            yield (reduced,)
        else:
            yield (reduced, *(_gradient(reduced, kernel, stack) for kernel in _SOBEL))


def _pixel_loss(a: Image.Image, b: Image.Image) -> float:
    with ImageChops.difference(a, b) as difference:
        return fmean(ImageStat.Stat(difference).mean) / _BYTE_RANGE


def _roi_loss(reference: tuple[Image.Image, ...], image: Image.Image) -> float:
    with _roi_features(image) as candidate:
        color = _pixel_loss(reference[0], candidate[0])
        if len(reference) == 1:
            return color
        gradient = fmean(_pixel_loss(a, b) for a, b in zip(reference[1:], candidate[1:], strict=True))
        return (color + gradient) / 2


def _regresses(baseline: Image.Image, candidate: Image.Image, tolerance: float) -> bool:
    with ImageChops.difference(baseline, candidate) as difference:
        return max(high for _, high in ImageStat.Stat(difference).extrema) / _BYTE_RANGE > tolerance


@dataclass(frozen=True)
class _Objective:
    boxes: tuple[RegionBox, ...]
    features: Mapping[RegionBox, tuple[Image.Image, ...]]
    targets: tuple[RegionBox, ...]
    anchors: tuple[RegionBox, ...]
    anchor_stats: tuple[tuple[float, ...], ...]
    guards: tuple[tuple[RegionBox, float], ...]

    def error(self, candidate: Mapping[RegionBox, Image.Image]) -> float:
        roi_error = fmean(_roi_loss(self.features[box], candidate[box]) for box in self.targets)
        if not self.anchors:
            return roi_error
        stats = _anchor_loss(self.anchor_stats, [candidate[box] for box in self.anchors])
        return (roi_error + stats) / 2

    def regresses(self, baseline: Mapping[RegionBox, Image.Image], candidate: Mapping[RegionBox, Image.Image]) -> bool:
        return any(_regresses(baseline[box], candidate[box], tolerance) for box, tolerance in self.guards)


def _validate_boxes(boxes: tuple[RegionBox, ...], width: int, height: int) -> None:
    if len(boxes) > _MAX_REGIONS or sum((right - left) * (bottom - top) for left, top, right, bottom in boxes) > _MAX_READ_PIXELS:
        msg = "Optimization readback is too large; select fewer or smaller target/protected regions"
        raise ValueError(msg)
    if any(right > width or bottom > height for _, _, right, bottom in boxes):
        msg = "Optimization regions must fit the fixed render dimensions"
        raise ValueError(msg)


@contextmanager
def prepare_objective(
    reference_png: bytes,
    width: int,
    height: int,
    regions: Sequence[PixelRegion],
    anchors: Sequence[PixelRegion],
    guards: Sequence[ProtectedRegion],
) -> Iterator[_Objective]:
    """预计算参考 ROI 的颜色和边缘, 并固定每次像素回读的位置与顺序."""
    targets = tuple(dict.fromkeys(map(_box, regions))) or ((0, 0, width, height),)
    anchor_boxes = tuple(dict.fromkeys(map(_box, anchors)))
    guard_boxes = tuple((_box(item.region), item.tolerance) for item in guards)
    boxes = tuple(dict.fromkeys((*targets, *anchor_boxes, *(box for box, _ in guard_boxes))))
    _validate_boxes(boxes, width, height)
    with ExitStack() as stack:
        image = stack.enter_context(Image.open(io.BytesIO(reference_png)))
        if image.format != "PNG" or image.size != (width, height):
            msg = "Optimization requires a reference PNG with the same width and height as the rendered canvas"
            raise ValueError(msg)
        if reference_png[24] == BIT_DEPTH:
            image = stack.enter_context(display_png16(reference_png))
        reference = {box: stack.enter_context(_display(stack.enter_context(image.crop(box)))) for box in boxes}
        features = {box: stack.enter_context(_roi_features(reference[box])) for box in targets}
        anchor_stats = tuple(_statistics(reference[box]) for box in anchor_boxes)
        yield _Objective(boxes, features, targets, anchor_boxes, anchor_stats, guard_boxes)


@contextmanager
def display_samples(boxes: tuple[RegionBox, ...], pixels: tuple[bytes, ...]) -> Iterator[dict[RegionBox, Image.Image]]:
    """将单次 RGBA 回读按原始坐标还原, 并按同一显示条件处理."""
    with ExitStack() as stack:
        samples = {}
        for box, data in zip(boxes, pixels, strict=True):
            image = stack.enter_context(Image.frombytes("RGBA", _size(box), data))
            samples[box] = stack.enter_context(_display(image))
        yield samples
