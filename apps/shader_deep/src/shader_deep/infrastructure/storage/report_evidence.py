"""可选证据文件的版本、包内路径与内容身份校验."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from shader_deep.domain.evidence import MeasurementRecord  # noqa: TC001  # Pydantic 在运行时构造证据记录.
from shader_deep.imaging.profiles import summarize_profile

RGB_CHANNELS = 3
ENCODED_MAXIMUM = 255

if TYPE_CHECKING:
    from shader_deep.domain.five_libraries import FiveLibraries


class EvidenceAsset(BaseModel):
    """只能携带包内 PNG, 声明原始字节哈希."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportEvidence(BaseModel):
    """F/R 的实际观察依据, 不替代五库选择或视觉验收."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["analysis_evidence_v1"] = "analysis_evidence_v1"
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[MeasurementRecord, ...]
    links: dict[str, tuple[str, ...]]
    assets: tuple[EvidenceAsset, ...] = ()


def evidence_path(directory: Path, name: str) -> Path:
    """拒绝绝对、穿越、反斜杠和符号链接逃逸路径."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name.startswith("evidence/") or path.suffix != ".png":
        msg = "证据制品路径必须是包内 evidence/*.png"
        raise ValueError(msg)
    resolved = (directory / name).resolve()
    if not resolved.is_relative_to(directory.resolve()):
        msg = "证据路径越出报告包"
        raise ValueError(msg)
    return resolved


def read_evidence(directory: Path, declaration: EvidenceAsset | None, libraries: FiveLibraries, reference_sha256: str) -> ReportEvidence | None:
    """未声明表示旧包无依据; 已声明但缺失或损坏明确拒绝."""
    if declaration is None:
        return None
    if declaration.path != "evidence.json":
        msg = "证据声明必须指向 evidence.json"
        raise ValueError(msg)
    source = (directory / declaration.path).resolve()
    if not source.is_relative_to(directory.resolve()):
        msg = "证据文件路径越出报告包"
        raise ValueError(msg)
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != declaration.sha256:
        msg = "证据文件哈希校验失败"
        raise ValueError(msg)
    evidence = ReportEvidence.model_validate_json(data)
    ids = {record.id for record in evidence.records}
    observations = {item.id for item in (*libraries.features, *libraries.relations)}
    if evidence.reference_sha256 != reference_sha256 or len(ids) != len(evidence.records) or not evidence.links.keys() <= observations:
        msg = "证据来源或观察关联不一致"
        raise ValueError(msg)
    used = {identity for identities in evidence.links.values() for identity in identities}
    # 所有测量都须被 F/R 使用, 所有关联也须能落到本包记录; 禁止无关证据随包混入.
    if used != ids or any(not identities or len(set(identities)) != len(identities) for identities in evidence.links.values()):
        msg = "证据关联存在未知、重复或无关记录"
        raise ValueError(msg)
    _check_records(directory, evidence, reference_sha256)
    return evidence


def evidence_view(evidence: ReportEvidence | None, selected: set[str]) -> JsonValue:
    """只读视图按 F/R 取材, 不改变完整证据文件."""
    if evidence is None:
        return None
    links = {key: ids for key, ids in evidence.links.items() if key in selected}
    ids = {identity for values in links.values() for identity in values}
    records = tuple(record for record in evidence.records if record.id in ids)
    paths = {record.artifact_path for record in records if record.artifact_path}
    return evidence.model_copy(
        update={"links": links, "records": records, "assets": tuple(asset for asset in evidence.assets if asset.path in paths)}
    ).model_dump(mode="json")


def evidence_bytes(evidence: ReportEvidence) -> bytes:
    """确定性序列化, manifest 和生成摘要使用完全相同字节."""
    return (json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _check_records(directory: Path, evidence: ReportEvidence, reference_sha256: str) -> None:
    """校验每个记录及其唯一制品声明."""
    assets = {asset.path: asset for asset in evidence.assets}
    if len(assets) != len(evidence.assets) or set(assets) != {record.artifact_path for record in evidence.records if record.artifact_path}:
        msg = "证据制品声明不完整"
        raise ValueError(msg)
    with Image.open(directory / "reference.png") as reference:
        image_size = reference.size
    for record in evidence.records:
        region = record.spec.region
        if (
            record.image_sha256 != reference_sha256
            or record.image_size != image_size
            or region.right > image_size[0]
            or region.bottom > image_size[1]
        ):
            msg = "测量原图身份或区域不一致"
            raise ValueError(msg)
    for asset in evidence.assets:
        if hashlib.sha256(evidence_path(directory, asset.path).read_bytes()).hexdigest() != asset.sha256:
            msg = "证据制品哈希校验失败"
            raise ValueError(msg)
    # 哈希只证明制品字节未变, 还须按测量类型检查像素内容或统计字段语义.
    for record in evidence.records:
        _check_measurement(directory, record)


def _check_measurement(directory: Path, record: MeasurementRecord) -> None:
    """按实际方法检查必需内容, 不把编码均值解释为物理量."""
    if record.method_version != "reference-pixels-v1" or record.alpha_policy != "composite_on_white_for_statistics":
        msg = "不支持的测量方法或透明度策略"
        raise ValueError(msg)
    if record.spec.kind == "crop":
        _check_crop(directory, record)
    else:
        _check_statistics(record)


def _check_crop(directory: Path, record: MeasurementRecord) -> None:
    """裁剪必须携带原图区域的真实 PNG 像素."""
    if (
        record.artifact_path is None
        or record.metrics != ("original_rgba_pixels",)
        or record.aggregation != "none"
        or record.samples_per_value != 1
        or record.units != "encoded_RGBA_0_255"
        or record.mean_rgb is not None
        or record.mean_luma is not None
        or record.profile
        or record.profile_digest is not None
    ):
        msg = "裁剪证据缺少实际图像或测量语义不一致"
        raise ValueError(msg)
    region = record.spec.region
    data = evidence_path(directory, record.artifact_path).read_bytes()
    try:
        # 从固定原图重新裁剪并逐像素比对 RGBA, 仅有相同尺寸或自洽哈希不能证明来源真实.
        with (
            Image.open(io.BytesIO(data)) as image,
            Image.open(directory / "reference.png") as reference,
            reference.crop((region.left, region.top, region.right, region.bottom)) as expected,
            image.convert("RGBA") as actual_pixels,
            expected.convert("RGBA") as expected_pixels,
        ):
            if image.format != "PNG" or image.size != expected.size or actual_pixels.tobytes() != expected_pixels.tobytes():
                msg = "裁剪证据不是固定原图区域的 PNG 像素"
                raise ValueError(msg)
    except OSError as error:
        msg = "裁剪证据无法解码为 PNG"
        raise ValueError(msg) from error


def _check_statistics(record: MeasurementRecord) -> None:
    """拒绝缺少数值、非有限值和改变聚合含义的统计记录."""
    # 此处检查数值范围和聚合契约, 不从原图重算均值, 也不把编码亮度当作物理光照结论.
    region = record.spec.region
    width, height = region.right - region.left, region.bottom - region.top
    if record.spec.kind == "region_stats":
        values = (*(record.mean_rgb or ()), record.mean_luma)
        valid = (
            record.mean_rgb is not None
            and record.mean_luma is not None
            and len(record.mean_rgb) == RGB_CHANNELS
            and not record.profile
            and record.profile_digest is None
        )
        aggregation, samples, units = "mean_over_all_pixels_in_region", width * height, "encoded_RGB_0_255"
        metrics = ("mean_rgb", "mean_encoded_luma")
    else:
        horizontal = record.spec.axis == "x"
        values = record.profile
        valid = (
            len(values) == (width if horizontal else height)
            and record.mean_rgb is None
            and record.mean_luma is None
            and _valid_profile_digest(record)
        )
        aggregation = "mean_over_each_column_in_region" if horizontal else "mean_over_each_row_in_region"
        samples, units, metrics = height if horizontal else width, "encoded_luma_0_255", ("mean_encoded_luma_profile",)
    if (
        not valid
        or any(value is None or not math.isfinite(value) or not 0 <= value <= ENCODED_MAXIMUM for value in values)
        or record.aggregation != aggregation
        or record.samples_per_value != samples
        or record.units != units
        or record.metrics != metrics
        or record.artifact_path is not None
        or not record.limitations
    ):
        msg = "统计证据数值或聚合语义不合法"
        raise ValueError(msg)


def _valid_profile_digest(record: MeasurementRecord) -> bool:
    """按已声明的确定性方法重建摘要, 拒绝非有限数值或越界位置."""
    if record.profile_digest is None or any(not math.isfinite(value) or not 0 <= value <= ENCODED_MAXIMUM for value in record.profile):
        return False
    region = record.spec.region
    offset = region.left if record.spec.axis == "x" else region.top
    return record.profile_digest == summarize_profile(record.profile, offset)
