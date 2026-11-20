"""从固定双图构造评审用的像素统计和视图覆盖证据."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.domain.evidence import ImageRegion, MeasurementRequest, MeasurementSpec
from shader_deep.domain.visual_review import (
    CoverageEvidence,
    PairedRegionStatistics,
    PixelRegion,
    RegionStatistics,
    ReviewEvidence,
    UnassessedItem,
)
from shader_deep.imaging.measurements import ReferenceMeasurements

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.visual_review import ReviewRequest


def _view(window: PixelRegion, local: PixelRegion | None, size: tuple[int, int], role: str) -> PixelRegion:
    if (window.right - window.left, window.bottom - window.top) != size:
        msg = f"{role}的坐标窗口尺寸与输入 PNG 不符, 当前映射只接受 1:1 像素平移"
        raise ValueError(msg)
    return (
        window
        if local is None
        else PixelRegion(
            left=local.left + window.left,
            top=local.top + window.top,
            right=local.right + window.left,
            bottom=local.bottom + window.top,
        )
    )


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
