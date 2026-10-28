"""沿已冻结来源映射汇集辅助材料, 与 V0/V1 一起发布."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from shader_deep.domain.evidence import MeasurementRecord
from shader_deep.infrastructure.storage.report_evidence import EvidenceAsset, ReportEvidence


def _material_object(value: object) -> dict[str, JsonValue]:
    """将辅助材料解析为严格的 JSON 对象."""
    return TypeAdapter(dict[str, JsonValue]).validate_python(value)


def package_materials(payload: dict[str, JsonValue], reference_sha256: str) -> tuple[ReportEvidence | None, dict[str, bytes]]:
    """从选定版本的固定辅助材料构造包内副本, 不读取其他版本."""
    records = TypeAdapter(tuple[MeasurementRecord, ...]).validate_python(payload.get("records", []))
    if not records:
        return None, {}
    contents, assets, rewritten = {}, [], []
    identities = _material_object(payload["assets"])
    for record in records:
        updated = record
        if record.artifact_path:
            # 优先采用实际呈现时冻结的字节, 后续临时文件变更或清理不能改变已接受证据.
            frozen = _material_object(payload.get("asset_contents", {})).get(record.artifact_path)
            data = base64.b64decode(str(frozen), validate=True) if frozen is not None else Path(record.artifact_path).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if identities.get(record.artifact_path) != digest:
                msg = "冻结后的测量制品发生改变"
                raise ValueError(msg)
            # 包内按内容寻址并改写记录路径, 让报告移动后仍可独立读取同一证据.
            path = "evidence/" + digest + ".png"
            contents[path] = data
            assets.append(EvidenceAsset(path=path, sha256=digest))
            updated = replace(record, artifact_path=path)
        rewritten.append(updated)
    links = TypeAdapter(dict[str, tuple[str, ...]]).validate_python(payload["links"])
    evidence = ReportEvidence(
        reference_sha256=reference_sha256, records=tuple(rewritten), links=links, assets=tuple({asset.path: asset for asset in assets}.values())
    )
    return evidence, contents
