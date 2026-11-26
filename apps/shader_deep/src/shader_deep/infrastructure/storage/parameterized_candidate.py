"""为复用同一基线代码的候选保存独立渲染记录."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shader_deep.domain.shader_parameters import ParameterSpec, bake_uniforms, resolve_uniforms
from shader_deep.infrastructure.storage.snapshots import write_snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.shader_parameters import UniformValue


@dataclass(frozen=True, kw_only=True)
class CandidateParameters:
    """本次渲染的固定参数与生效值."""

    specs: tuple[ParameterSpec, ...]
    uniforms: dict[str, UniformValue]

    def save(self, record: Path, code: Path, preview: Path, *, time: float) -> None:
        """登记候选 PNG、基线代码和实际生效的外部输入."""
        write_snapshot(
            record,
            {
                "label": preview.stem,
                "png": preview.name,
                "glsl": code.name,
                "source_agent": "generation",
                "render": {"time": time, "uniforms": self.uniforms},
                "parameter_specs": [spec.model_dump(mode="json") for spec in self.specs],
                "glsl_sha256": hashlib.sha256(code.read_bytes()).hexdigest(),
                "png_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
            },
            temporary_suffix=".json.tmp",
        )


def _read_parameters(record: bytes, code: Path, glsl: bytes, preview: Path, png: bytes) -> CandidateParameters:
    payload = json.loads(record)
    for label, path, content in (("glsl", code, glsl), ("png", preview, png)):
        if payload[label] != path.name or payload[f"{label}_sha256"] != hashlib.sha256(content).hexdigest():
            msg = f"{label} no longer matches its parameterized render record"
            raise ValueError(msg)
    specs = tuple(ParameterSpec.model_validate(spec) for spec in payload["parameter_specs"])
    values = payload["render"]["uniforms"]
    if not isinstance(values, dict) or values.keys() != {spec.name for spec in specs}:
        msg = "Render record must include every effective uniform value"
        raise ValueError(msg)
    return CandidateParameters(specs=specs, uniforms=resolve_uniforms(specs, values))


def read_candidate_parameters(record: Path, code: Path, preview: Path) -> CandidateParameters:
    """校验参数记录的制品身份后恢复生效参数."""
    return _read_parameters(record.read_bytes(), code, code.read_bytes(), preview, preview.read_bytes())


def read_baked_candidate(record: Path, code: Path, preview: Path) -> tuple[str, bytes]:
    """从同一对已校验的源码与预览字节恢复实际渲染的单文件版本.

    Args:
        record: 候选的外置参数记录.
        code: 本次渲染使用的参数化 GLSL.
        preview: 本次渲染导出的 PNG.

    Returns:
        以实际生效值烘焙的代码及其对应预览.

    Raises:
        ValueError: 制品名称、哈希、参数规格或赋值与渲染记录不符.
    """
    glsl, png = code.read_bytes(), preview.read_bytes()
    params = _read_parameters(record.read_bytes(), code, glsl, preview, png)
    text = glsl.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return bake_uniforms(text, params.specs, params.uniforms), png


def save_baked_candidate(record: Path, code: Path, preview: Path) -> Path:
    """从实际渲染记录生成单文件, 并保留参数化代码及记录."""
    baked, _ = read_baked_candidate(record, code, preview)
    path = record.with_name(f"{preview.stem}.baked.glsl")
    path.write_text(baked, encoding="utf-8")
    return path
