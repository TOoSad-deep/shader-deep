"""观察者的有限参考图测量和图像回看."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from shader_deep.domain.evidence import MeasurementRequest, MeasurementSpec
from shader_deep.domain.scene_observations import REFERENCE_MEASUREMENT_ARCHIVE_LIMIT, REFERENCE_MEASUREMENT_LIMIT, ReferenceMeasurement
from shader_deep.imaging.measurements import ReferenceMeasurements
from shader_deep.infrastructure.llm.messages import png_data_url

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.evidence import ImageRegion

MAX_CROP_PIXELS = 1_048_576


class ObservationMeasurements:
    """只提供裁剪与区域统计, 沿用固定原图和标准测量缓存."""

    def __init__(self, data: bytes, directory: Path, reference_sha256: str, *, inherited: tuple[ReferenceMeasurement, ...] = ()) -> None:
        """延迟创建像素引擎, 普通观察只使用整图."""
        self._data, self._directory, self._reference_sha256 = data, directory, reference_sha256
        self._engine: ReferenceMeasurements | None = None
        self._anchors = {(item.kind, item.region): item for item in inherited}
        self._crops: dict[str, str] = {}

    @property
    def anchors(self) -> tuple[ReferenceMeasurement, ...]:
        """返回有序去重记录, 每项都由程序从固定 PNG 生成."""
        return tuple(self._anchors.values())

    @property
    def crop_images(self) -> tuple[tuple[str, str], ...]:
        """返回与锚点 ID 绑定的裁图 data URL, 供实际请求注入."""
        return tuple(self._crops.items())

    def _measure(self, kind: Literal["crop", "region_stats"], region: ImageRegion, question: str) -> tuple[ReferenceMeasurement, bool]:
        key = kind, region
        if kind == "crop" and (region.right - region.left) * (region.bottom - region.top) > MAX_CROP_PIXELS:
            msg = "裁剪至多包含 1048576 个像素, 请选择局部区域或直接使用请求中的整图"
            raise ValueError(msg)
        if key not in self._anchors and len(self._anchors) >= REFERENCE_MEASUREMENT_ARCHIVE_LIMIT:
            msg = "观察包的测量锚点已达上限, 请复用已保存的规格"
            raise ValueError(msg)
        if self._engine is None:
            self._engine = ReferenceMeasurements(self._data, self._directory, "A1", self._reference_sha256, REFERENCE_MEASUREMENT_LIMIT)
        record, cached = self._engine.measure(MeasurementRequest(question=question, measurement=MeasurementSpec(kind=kind, region=region)))
        anchor = ReferenceMeasurement(
            id=record.id,
            kind=kind,
            region=region,
            question=record.question,
            image_sha256=record.image_sha256,
            mean_rgb=record.mean_rgb,
            mean_luma=record.mean_luma,
        )
        old = self._anchors.get(key)
        if old is not None and old.semantic_key() == anchor.semantic_key():
            anchor = old
        if record.artifact_path is not None and anchor.id not in self._crops:
            self._crops[anchor.id] = png_data_url(Path(record.artifact_path))
        if old is not None and old != anchor:
            self._anchors.pop(key)
        self._anchors[key] = anchor
        return anchor, cached

    def measure(self, kind: Literal["crop", "region_stats"], region: ImageRegion, question: str) -> dict[str, JsonValue]:
        """执行受限操作, 同规格同结果沿用旧锚点 ID."""
        anchor, cached = self._measure(kind, region, question)
        return {
            "measurement": anchor.model_dump(mode="json", exclude_none=True),
            "cached": cached,
            "remaining": REFERENCE_MEASUREMENT_LIMIT - (len(self._engine.records) if self._engine else 0),
        }
