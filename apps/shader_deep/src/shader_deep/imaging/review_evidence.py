"""从固定双图构造评审用的像素统计和视图覆盖证据."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from PIL import Image, ImageDraw, ImageFont

from shader_deep.domain.evidence import ImageRegion, MeasurementRequest, MeasurementSpec
from shader_deep.domain.visual_review import (
    CoverageEvidence,
    EvidenceGap,
    PairedRegionStatistics,
    PixelRegion,
    RegionStatistics,
    ReviewEvidence,
    ReviewSample,
    UnassessedItem,
)
from shader_deep.imaging.measurements import ReferenceMeasurements
from shader_deep.imaging.png16 import BIT_DEPTH, display_png16
from shader_deep.imaging.png_chunks import GRAYSCALE_COLOR_TYPES, copy_png_color_chunks, expand_low_depth_gray, is_low_depth_gray

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.scene_observations import ReferenceMeasurement
    from shader_deep.domain.visual_review import ReviewRequest

_ROLE_BAR_PIXEL_BUDGET = 1_000_000
_ROLE_BAR_HEIGHT = 32


@dataclass(frozen=True, kw_only=True)
class WorkflowEvidence:
    """工作流转交的图像证据选择, 坐标均指向真实参考图或共用画布."""

    reference_measurements: tuple[ReferenceMeasurement, ...] = ()
    optimization_regions: tuple[PixelRegion, ...] = ()
    resample_regions: tuple[PixelRegion, ...] = ()
    expand_views: bool = False
    rendered_time: float | None = None


def _shift(region: PixelRegion, x: int, y: int) -> PixelRegion:
    return PixelRegion(left=region.left + x, top=region.top + y, right=region.right + x, bottom=region.bottom + y)


def _view(window: PixelRegion, local: PixelRegion | None, size: tuple[int, int], role: str) -> PixelRegion:
    if (window.right - window.left, window.bottom - window.top) != size:
        msg = f"{role}的坐标窗口尺寸与输入 PNG 不符, 当前映射只接受 1:1 像素平移"
        raise ValueError(msg)
    return window if local is None else _shift(local, window.left, window.top)


def _windows(request: ReviewRequest, sizes: tuple[tuple[int, int], tuple[int, int]]) -> tuple[PixelRegion, PixelRegion] | None:
    if request.coverage is not None:
        return request.coverage.reference_window, request.coverage.rendered_window
    if sizes[0] != sizes[1]:
        return None
    bounds = PixelRegion(left=0, top=0, right=sizes[0][0], bottom=sizes[0][1])
    return bounds, bounds


def _paired_regions(evidence: WorkflowEvidence, reference_window: PixelRegion, reference_sha256: str) -> tuple[tuple[str, PixelRegion], ...]:
    regions = []
    for item in evidence.reference_measurements:
        if item.image_sha256 != reference_sha256:
            msg = f"参考锚点 {item.id} 的图像身份与当前参考 PNG 不一致"
            raise ValueError(msg)
        if item.kind != "region_stats":
            continue
        region = PixelRegion(left=item.region.left, top=item.region.top, right=item.region.right, bottom=item.region.bottom)
        regions.append((f"参考锚点 {item.id}", _shift(region, reference_window.left, reference_window.top)))
    regions.extend((f"调参区域 {index}", region) for index, region in enumerate(evidence.optimization_regions, start=1))
    regions.extend((f"补采区域 {index}", region) for index, region in enumerate(evidence.resample_regions, start=1))
    return tuple(regions)


def _append_pairs(
    existing: tuple[ReviewSample, ...],
    pairs: tuple[tuple[str, PixelRegion], ...],
    reference: PixelRegion,
    rendered: PixelRegion,
    windows: tuple[PixelRegion, PixelRegion],
) -> tuple[ReviewSample, ...]:
    labels = {item.label for item in existing}
    seen = {(item.reference_region, item.rendered_region) for item in existing}
    samples = list(existing)
    for label, region in pairs:
        if label in labels or not (reference.covers(region) and rendered.covers(region)):
            continue
        ref, candidate = (_shift(region, -window.left, -window.top) for window in windows)
        if (ref, candidate) not in seen:
            samples.append(ReviewSample(label=label, reference_region=ref, rendered_region=candidate))
            seen.add((ref, candidate))
            labels.add(label)
    return tuple(samples)


def classify_review_gaps(
    request: ReviewRequest, sizes: tuple[tuple[int, int], tuple[int, int]], unassessed: tuple[UnassessedItem, ...], coverage: CoverageEvidence | None
) -> dict[str, EvidenceGap]:
    """按实际画布确认未评原因, 只有双方已有像素才可自动扩窗或补测."""
    windows = _windows(request, sizes)
    locked = {item.id: item for item in coverage.unassessed} if coverage else {}
    regions = {(row.dimension, row.check): row.region for row in request.coverage.required_regions} if request.coverage else {}
    classified = {}
    for item in unassessed:
        gap = item.evidence_gap
        region = regions.get((item.dimension, item.subject))
        if item.id in locked:
            recoverable = windows is not None and region is not None and all(window.covers(region) for window in windows)
            gap = EvidenceGap(kind="insufficient_crop_or_sample") if recoverable else None
        elif gap is not None and gap.kind == "insufficient_crop_or_sample":
            region = gap.sample_region or region
            if windows is not None and region is not None and all(window.covers(region) for window in windows):
                gap = EvidenceGap(kind=gap.kind, sample_region=region)
            else:
                gap = None
        classified[item.id] = gap or EvidenceGap(kind="missing_context_or_frame")
    return classified


def missing_sample_regions(
    request: ReviewRequest, regions: tuple[PixelRegion, ...], sizes: tuple[tuple[int, int], tuple[int, int]]
) -> tuple[PixelRegion, ...]:
    """丢弃在两幅原图同一位置已量过的采样请求, 避免无变化的复评."""
    windows = _windows(request, sizes)
    if windows is None:
        return ()
    measured = {
        (_shift(sample.reference_region, windows[0].left, windows[0].top), _shift(sample.rendered_region, windows[1].left, windows[1].top))
        for sample in request.samples
    }
    return tuple(ref for ref in dict.fromkeys(regions) if (ref, ref) not in measured)


def inherit_workflow_samples(
    request: ReviewRequest, sizes: tuple[tuple[int, int], tuple[int, int]], reference_sha256: str, evidence: WorkflowEvidence
) -> tuple[ReviewSample, ...]:
    """仅在两图均完整覆盖且坐标可比时, 从上游矩形派生成对采样.

    Args:
        request: 已应用实际视图窗口的本轮任务.
        sizes: 两张固定 PNG 的原始尺寸.
        reference_sha256: 当前固定参考 PNG 的哈希.
        evidence: 来自参考锚点和调参的待补充测量位置.

    Returns:
        原有采样与新增可比采样, 新增区域各自映射到原始 PNG.

    Raises:
        ValueError: 坐标窗口无效, 或锚点引用与当前参考图不同.
    """
    windows = _windows(request, sizes)
    if windows is None or not (evidence.reference_measurements or evidence.optimization_regions or evidence.resample_regions):
        return request.samples
    reference = _view(windows[0], request.reference_region, sizes[0], "参考图")
    rendered = _view(windows[1], request.rendered_region, sizes[1], "候选图")
    pairs = _paired_regions(evidence, windows[0], reference_sha256)
    return _append_pairs(request.samples, pairs, reference, rendered, windows)


def _display_rgba(original: Image.Image, content: bytes) -> Image.Image:
    if content[24] == BIT_DEPTH:
        return display_png16(content)
    if is_low_depth_gray(content):
        with expand_low_depth_gray(original, content) as gray:
            return gray.convert("RGBA")
    return original.convert("RGBA")


def label_review_png(content: bytes, role: Literal["reference", "rendered"]) -> bytes:
    """在原图像素上方增加角色栏, 仅供模型展示使用.

    Args:
        content: 已校验的参考或候选 PNG.
        role: 由程序绑定的展示角色.

    Returns:
        带角色栏的 PNG, 栏下图像保持原尺寸、方向和颜色空间.
    """
    label, color = ("REFERENCE", "#153950") if role == "reference" else ("CANDIDATE", "#663c1d")
    font = ImageFont.load_default(size=15)
    with Image.open(io.BytesIO(content)) as source, _display_rgba(source, content) as image:
        # 给小图补足角色栏宽度. 超长窄图避免横向扩展导致分配过大.
        padded = max(image.width, int(font.getlength(label)) + 16)
        width = padded if (padded - image.width) * image.height < _ROLE_BAR_PIXEL_BUDGET else image.width
        bar = min(_ROLE_BAR_HEIGHT, max(1, _ROLE_BAR_PIXEL_BUDGET // width))
        # 灰度 PNG 的 ICC 可只描述灰度通道, 加栏后的 PNG 仍用灰度色型.
        mode = "LA" if content[25] in GRAYSCALE_COLOR_TYPES else "RGBA"
        with Image.new(mode, (width, image.height + bar)) as output:
            draw = ImageDraw.Draw(output)
            draw.rectangle((0, 0, width - 1, bar - 1), fill=color)
            draw.text((8, 5), label, fill="white", font=font)
            output.paste(image, (0, bar))
            buffer = io.BytesIO()
            output.save(buffer, format="PNG")
    # 展示图使用新的 8 位样本布局, sBIT 仍只适用于原 PNG.
    return copy_png_color_chunks(content, buffer.getvalue(), preserve_significant_bits=False)


def _coverage(request: ReviewRequest, sizes: tuple[tuple[int, int], tuple[int, int]]) -> CoverageEvidence | None:
    spec = request.coverage
    if spec is None:
        return None
    reference = _view(spec.reference_window, request.reference_region, sizes[0], "参考图")
    rendered = _view(spec.rendered_window, request.rendered_region, sizes[1], "候选图")
    gaps = []
    for index, required in enumerate(spec.required_regions, start=1):
        missing = [role for role, view in (("参考", reference), ("候选", rendered)) if not view.covers(required.region)]
        if missing:
            roles = "和".join(missing)
            reason = f"{roles}视图未完整覆盖「{required.check}」的必查区域, 需要提供包含该部位的完整视图。"
            gaps.append(UnassessedItem(id=f"program-coverage-{index}", dimension=required.dimension, subject=required.check, reason=reason))
    return CoverageEvidence(reference_view=reference, rendered_view=rendered, unassessed=tuple(gaps))


def _stats(engine: ReferenceMeasurements, region: PixelRegion, label: str, view: PixelRegion | None, role: str) -> RegionStatistics:
    if view is not None and not view.covers(region):
        msg = f"{role}的统计区域「{label}」超出本次局部评审视图"
        raise ValueError(msg)
    spec = MeasurementSpec(kind="region_stats", region=ImageRegion(left=region.left, top=region.top, right=region.right, bottom=region.bottom))
    result, _ = engine.measure(MeasurementRequest(question=label, measurement=spec))
    if result.mean_rgb is None or result.mean_luma is None or result.samples_per_value is None:
        msg = f"{role}的统计区域未生成完整的像素统计: {label}"
        raise ValueError(msg)
    return RegionStatistics(region=region, mean_rgb=result.mean_rgb, mean_luma=result.mean_luma, pixel_count=result.samples_per_value)


def _samples(request: ReviewRequest, images: tuple[bytes, bytes], directory: Path) -> tuple[PairedRegionStatistics, ...]:
    count = len(request.samples)
    if not count:
        return ()
    reference = ReferenceMeasurements(images[0], directory, "review", "reference", count)
    rendered = ReferenceMeasurements(images[1], directory, "review", "rendered", count)
    return tuple(
        PairedRegionStatistics(
            label=item.label,
            reference=_stats(reference, item.reference_region, item.label, request.reference_region, "参考图"),
            rendered=_stats(rendered, item.rendered_region, item.label, request.rendered_region, "候选图"),
        )
        for item in request.samples
    )


def measure_review_evidence(
    request: ReviewRequest, images: tuple[bytes, bytes], sizes: tuple[tuple[int, int], tuple[int, int]], directory: Path
) -> ReviewEvidence | None:
    """按任务启用的区域测量和 1:1 视图映射计算固定证据."""
    if not request.samples and request.coverage is None:
        return None
    return ReviewEvidence(coverage=_coverage(request, sizes), samples=_samples(request, images, directory))
