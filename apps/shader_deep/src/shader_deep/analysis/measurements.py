"""由协调器执行的小型确定性图像操作."""

from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from typing import TYPE_CHECKING

from PIL import Image, ImageStat

from shader_deep.analysis.evidence import MeasurementRecord, MeasurementRequest, MeasurementSpec
from shader_deep.analysis.profiles import summarize_profile

if TYPE_CHECKING:
    from pathlib import Path

MAX_IMAGE_PIXELS = 16_777_216
MAX_PROFILE_LENGTH = 4096


class ReferenceMeasurements:
    """管理固定参考图字节, 按测量规格去重, 并限制独立测量次数."""

    def __init__(self, data: bytes, directory: Path, parent_id: str, target_version: str, limit: int) -> None:
        """保存固定输入字节, 读取尺寸后立即关闭图像解码资源.

        Args:
            data: 本次运行启动时读取的参考 PNG 字节.
            directory: 本次运行的制品目录.
            parent_id: 主分析任务标识.
            target_version: 本次测量绑定的目标版本.
            limit: 本次运行允许的独立测量操作数上限.
        """
        self.data, self.directory = data, directory
        self.parent_id, self.target_version, self.limit = parent_id, target_version, limit
        self.image_sha256 = hashlib.sha256(data).hexdigest()
        with Image.open(io.BytesIO(data)) as image:
            self.image_size = image.size
            if image.width * image.height > MAX_IMAGE_PIXELS:
                msg = "Reference exceeds the measurement pixel limit"
                raise ValueError(msg)
        self.records: dict[MeasurementSpec, MeasurementRecord] = {}
        self.calls: list[dict[str, object]] = []

    def _canonical(self, spec: MeasurementSpec) -> MeasurementSpec:
        """检查原图边界与剖面长度, 归一化不影响操作结果的参数."""
        region = spec.region
        if region.right > self.image_size[0] or region.bottom > self.image_size[1]:
            msg = "Region is outside the original image; use original pixel coordinates"
            raise ValueError(msg)
        length = region.right - region.left if spec.axis == "x" else region.bottom - region.top
        if spec.kind == "line_profile" and length > MAX_PROFILE_LENGTH:
            msg = "Profile is too long; select a region spanning at most 4096 pixels along its axis"
            raise ValueError(msg)
        # axis 只影响剖面; 裁剪和区域统计统一为 x, 防止无关参数导致重复扣预算.
        return MeasurementSpec(kind=spec.kind, region=region, axis=spec.axis if spec.kind == "line_profile" else "x")

    def _pixels(self, spec: MeasurementSpec) -> Image.Image:
        """从固定字节读取区域, 返回由调用方负责关闭的独立 RGBA 图像."""
        region = spec.region
        with Image.open(io.BytesIO(self.data)) as source, source.crop((region.left, region.top, region.right, region.bottom)) as cropped:
            return cropped.convert("RGBA")

    def _execute(self, spec: MeasurementSpec, question: str) -> MeasurementRecord:
        """执行一次新测量, 绑定原图哈希、任务归属和坐标规格以便追溯."""
        identifier = f"{self.directory.name}-e{len(self.records) + 1:03d}"
        record = MeasurementRecord(
            id=identifier,
            parent_task_id=self.parent_id,
            target_version=self.target_version,
            image_sha256=self.image_sha256,
            spec=spec,
            question=question,
            image_size=self.image_size,
        )
        with self._pixels(spec) as pixels:
            # 裁剪保留原始透明度, 用于局部视觉查看, 不生成数值亮度结论.
            if spec.kind == "crop":
                folder = self.directory / "evidence"
                folder.mkdir(exist_ok=True)
                path = folder / f"{identifier}.png"
                pixels.save(path)
                return replace(record, artifact_path=str(path.resolve()))
            # 统计明确采用白底合成, 避免透明像素中不可见的 RGB 值误导均值.
            with Image.new("RGBA", pixels.size, "white") as background:
                background.alpha_composite(pixels)
                with background.convert("RGB") as rgb:
                    return _statistics(record, rgb)

    def measure(self, request: MeasurementRequest) -> tuple[MeasurementRecord, bool]:
        """执行测量或复用同规格结果, 缓存命中不消耗独立操作预算.

        Args:
            request: 已通过结构校验的测量规格及目的.

        Returns:
            程序生成的证据记录, 以及是否命中已有缓存.

        Raises:
            ValueError: 区域越界、操作尺寸超限或测量预算不足.
        """
        spec = self._canonical(request.measurement)
        # 缓存键只含实际操作参数; 不同问题请求同一测量时复用相同证据 ID.
        # calls 仍记录每次请求的问题和缓存命中情况, 便于核对预算与证据复用.
        cached = spec in self.records
        if not cached:
            if len(self.records) >= self.limit:
                msg = "Unique measurement budget exhausted; reuse existing evidence or preserve the uncertainty"
                raise ValueError(msg)
            self.records[spec] = self._execute(spec, request.question)
        result = self.records[spec]
        self.calls.append({"evidence_id": result.id, "question": request.question, "cached": cached})
        return result, cached


def _statistics(record: MeasurementRecord, rgb: Image.Image) -> MeasurementRecord:
    """计算区域均值, 或沿选定轴逐行、逐列平均形成一维亮度剖面."""
    means = ImageStat.Stat(rgb).mean
    if record.spec.kind == "region_stats":
        return replace(record, mean_rgb=(means[0], means[1], means[2]), mean_luma=_luma(means))
    # x 剖面逐列平均区域的全部行, y 剖面逐行平均全部列; 不是默认只取中心线.
    horizontal = record.spec.axis == "x"
    values = []
    for index in range(rgb.width if horizontal else rgb.height):
        box = (index, 0, index + 1, rgb.height) if horizontal else (0, index, rgb.width, index + 1)
        with rgb.crop(box) as strip:
            values.append(_luma(ImageStat.Stat(strip).mean))
    # 将区域内部下标还原成原图坐标, 模型可直接用摘要中的 position 定位.
    offset = record.spec.region.left if horizontal else record.spec.region.top
    return replace(record, profile=tuple(values), profile_digest=summarize_profile(tuple(values), offset))


def _luma(rgb: list[float]) -> float:
    """对编码 RGB 值加权得到亮度近似值, 不执行线性化或物理光度测量."""
    return round(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2], 3)
