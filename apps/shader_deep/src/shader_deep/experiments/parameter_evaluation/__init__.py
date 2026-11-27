"""独立事后评估入口; 数值排序、视觉判断和用户验收保持分离."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shader_deep.experiments.parameter_evaluation._judge import parse_judge_result, prepare_judge_request
from shader_deep.experiments.parameter_evaluation._metrics import background_metrics, decode_png, region_metrics

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ReferenceSpec", "build_judge_request", "evaluate_render", "freeze_reference", "parse_judge_result"]

REFERENCE_SHA256 = "8431f0a97a9caae5bdd1ad7e0d6d82e94a604c5a53c49cb9abd659f3f761a41a"
PROTOCOL = "circles-parameter-evaluation-v2"
REGIONS = (("full", (0, 0, 1024, 576)), ("foreground", (330, 145, 695, 435)), ("overlap", (450, 190, 580, 385)))


@dataclass(frozen=True)
class ReferenceSpec:
    """已确认身份的参考字节; 路径只作来源记录, 后续不重新读参考文件.

    Attributes:
        png: 已冻结 PNG 字节.
        sha256: PNG 字节哈希.
        source: 原始来源绝对路径.
    """

    png: bytes
    sha256: str
    source: str


def freeze_reference(reference_path: Path) -> ReferenceSpec:
    """冻结本实验唯一参考, 拒绝其他尺寸或内容.

    Args:
        reference_path: circles 基准参考图的路径.

    Returns:
        只含冻结字节和来源的参考规格.

    Raises:
        ValueError: 文件身份或尺寸与固定协议不符.
    """
    png = reference_path.read_bytes()
    digest = hashlib.sha256(png).hexdigest()
    with decode_png(png) as image:
        if digest != REFERENCE_SHA256 or image.size != (1024, 576):
            msg = "Reference does not match the frozen circles benchmark"
            raise ValueError(msg)
    return ReferenceSpec(png=png, sha256=digest, source=str(reference_path.resolve()))


def _pair(spec: ReferenceSpec, candidate: bytes) -> dict[str, str]:
    if spec.sha256 != REFERENCE_SHA256 or hashlib.sha256(spec.png).hexdigest() != spec.sha256:
        msg = "Reference bytes no longer match the frozen benchmark"
        raise ValueError(msg)
    hashes = {"protocol": PROTOCOL, "reference_sha256": spec.sha256, "candidate_sha256": hashlib.sha256(candidate).hexdigest()}
    return {**hashes, "pair_id": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def evaluate_render(spec: ReferenceSpec, render_path: Path) -> dict[str, object]:
    """评估单张原尺度渲染, 不产生通过阈值或执行成功结论.

    Args:
        spec: 冻结的参考规格.
        render_path: 最终渲染或明确标识的历史渲染.

    Returns:
        包含双图哈希、固定矩形及数值诊断的可序列化记录.

    Raises:
        ValueError: 参考身份、PNG 内容或尺寸不符.
    """
    candidate = render_path.read_bytes()
    identity = _pair(spec, candidate)
    with decode_png(spec.png) as reference, decode_png(candidate) as current:
        if current.size != reference.size:
            msg = "Evaluation dimensions must match; resizing and alignment are forbidden"
            raise ValueError(msg)
        regions = {name: region_metrics(reference, current, rectangle) for name, rectangle in REGIONS}
        regions["background"] = background_metrics(reference, current, REGIONS[1][1])
    return {
        **identity,
        "regions": regions,
        "metric_range": "RGB error normalized to [0,1]; SSIM higher is closer",
        "ssim_variant": "Encoded BT.601 luma; 7x7 uniform valid windows; sample covariance; K1=.01,K2=.03,data_range=255",
        "visual_verdict": "unassessed",
        "acceptance": "uncalibrated",
        "roi_provenance": "Fixed from frozen reference before candidate inspection; top-left origin, right/bottom exclusive",
    }


def build_judge_request(spec: ReferenceSpec, render_path: Path) -> dict[str, object]:
    """生成冻结双图和 rubric 的盲评请求, 不执行模型调用.

    Args:
        spec: 已冻结参考图.
        render_path: 最终渲染路径, 路径名不会进入模型请求.

    Returns:
        含 messages、schema 和 pair_id 的独立评审请求记录.

    Raises:
        ValueError: 参考身份、PNG 内容或渲染尺寸无效.
    """
    candidate = render_path.read_bytes()
    identity = _pair(spec, candidate)
    with decode_png(candidate) as image:
        if image.size != (1024, 576):
            msg = "Judge requires the original 1024x576 rendering"
            raise ValueError(msg)
    return prepare_judge_request(spec.png, candidate, identity)
