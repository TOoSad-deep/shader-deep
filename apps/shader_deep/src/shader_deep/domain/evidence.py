"""针对固定参考图的测量契约, 测量操作由协调器执行."""

from __future__ import annotations

from dataclasses import dataclass as record
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints
from pydantic.dataclasses import dataclass

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Pixel = Annotated[int, Field(strict=True, ge=0)]
# strict=True 拒绝布尔值及隐式数值转换; 区域坐标必须是原图中的非负整数像素.
CONFIG = ConfigDict(extra="forbid")


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class ImageRegion:
    """原图像素区域, 以左上角为原点, 右边界和下边界不包含在区域内."""

    # 例如单像素区域为 [left, left+1) 和 [top, top+1), 与 Pillow 裁剪边界一致.
    # 本类型只校验宽高为正; 是否超出原图尺寸由测量执行器检查.
    left: Pixel
    top: Pixel
    right: Pixel
    bottom: Pixel

    def __post_init__(self) -> None:
        """读取图像前拒绝空区域或方向颠倒的区域边界."""
        if self.right <= self.left or self.bottom <= self.top:
            msg = "Region must have positive width and height; for one pixel row/column use bottom=top+1 or right=left+1"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class MeasurementSpec:
    """受预算限制的测量规格; 剖面轴指定沿哪个像素坐标方向采样."""

    kind: Literal["crop", "region_stats", "line_profile"]
    region: ImageRegion
    axis: Literal["x", "y"] = "x"


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class EvidenceRequest:
    """子任务提出的证据建议, 不赋予测量执行权, 也不预设证据结果 ID."""

    question: Text
    why_it_matters: Text
    measurement: MeasurementSpec | None = None


@dataclass(frozen=True, kw_only=True, config=CONFIG)
class MeasurementRequest:
    """主分析 Agent 请求执行的测量, 以及该测量需要回答的问题."""

    question: Text
    measurement: MeasurementSpec


@record(frozen=True, kw_only=True)
class ProfilePoint:
    """程序计算的采样点, 记录原图轴坐标及编码颜色的亮度近似值."""

    position: int
    value: float
    contrast: float = 0.0
    neighbor_level: float | None = None


@record(frozen=True, kw_only=True)
class ProfileDigest:
    """带原图位置的样本与局部极值候选, 不附加网格线等语义标签."""

    count: int
    samples: tuple[ProfilePoint, ...]
    dark_extrema: tuple[ProfilePoint, ...]
    bright_extrema: tuple[ProfilePoint, ...]
    # 摘要对极值数量设上限, 截断标记说明还有候选未展示; 完整剖面仍在 MeasurementRecord.
    extrema_truncated: bool
    radius_pixels: int = 3
    minimum_contrast: float = 1.0
    method_version: str = "two-sided-extrema-v3"
    contrast_definition: str = "Minimum directional difference against BOTH samples at +/- radius; not contrast against an unoccluded background."


@record(frozen=True, kw_only=True)
class MeasurementRecord:
    """程序生成的测量证据, 数值仅描述实际采样区域.

    `mean_luma` 和 `profile` 对编码 RGB 值加权, 不是物理亮度测量.
    统计时将透明像素合成到白底; 裁剪图保留原始像素的颜色与透明度.
    引用裁剪图作为视觉依据前, 需要实际查看其图像内容.
    """

    id: str
    parent_task_id: str
    target_version: str
    # 用原始输入字节哈希绑定证据来源, 避免只凭文件名认定是同一张参考图.
    image_sha256: str
    spec: MeasurementSpec
    question: str
    image_size: tuple[int, int]
    method_version: str = "reference-pixels-v1"
    alpha_policy: str = "composite_on_white_for_statistics"
    mean_rgb: tuple[float, float, float] | None = None
    mean_luma: float | None = None
    profile: tuple[float, ...] = ()
    profile_digest: ProfileDigest | None = None
    artifact_path: str | None = None
    # 旧快照缺少下列字段时表示未提供语义, 不根据 kind 静默赋予新的测量能力.
    metrics: tuple[str, ...] = ()
    aggregation: str | None = None
    samples_per_value: int | None = None
    units: str | None = None
    limitations: tuple[str, ...] = ()
