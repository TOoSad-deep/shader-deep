"""在固定候选上进行直接调参或换帧, 供控制面使用."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.domain.tasks import CandidateRecord
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters, read_candidate_parameters, save_baked_candidate
from shader_deep.infrastructure.storage.planning_inputs import reference_size
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.optimization import optimize_uniforms
from shader_deep.rendering import WebGL2Renderer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import JsonValue

    from shader_deep.domain.visual_review import PixelRegion
    from shader_deep.optimization import ProtectedRegion
    from shader_deep.optimization.uniforms import TurningPoint


@dataclass(frozen=True, kw_only=True)
class NumericRevision:
    """新候选和诊断, 瓶颈时保存诊断但无新候选."""

    run_dir: Path
    candidate: CandidateRecord | None
    baked_code_path: Path | None
    diagnostics: dict[str, JsonValue]
    turning_points: tuple[CandidateRecord, ...] = ()


def _publish_turning_points(
    directory: Path, source: CandidateRecord, params: CandidateParameters, points: tuple[TurningPoint, ...], time: float
) -> tuple[CandidateRecord, ...]:
    records = []
    for point in points:
        preview = directory / f"candidate-e{point.evaluation}.png"
        code, sidecar = directory / "shader.glsl", preview.with_suffix(".parameters.json")
        preview.write_bytes(point.png)
        CandidateParameters(specs=params.specs, uniforms=point.uniforms).save(sidecar, code, preview, time=time)
        records.append(
            CandidateRecord(
                id=f"{directory.name}-e{point.evaluation}",
                task_id=source.task_id,
                code_path=str(code),
                preview_path=str(preview),
                render_record_path=str(sidecar),
            )
        )
    return tuple(records)


def _candidate_params(candidate: CandidateRecord) -> CandidateParameters | None:
    if candidate.render_record_path is None:
        return None
    if candidate.preview_path is None:
        msg = "参数化候选没有正式 PNG"
        raise ValueError(msg)
    return read_candidate_parameters(Path(candidate.render_record_path), Path(candidate.code_path), Path(candidate.preview_path))


def _publish(
    directory: Path,
    source: CandidateRecord,
    glsl: str,
    png: bytes | None,
    params: CandidateParameters | None,
    diagnostics: dict[str, JsonValue],
    time: float,
    points: tuple[TurningPoint, ...] = (),
) -> NumericRevision:
    output, baked = None, None
    if png is not None:
        code, preview = directory / "shader.glsl", directory / "candidate.png"
        code.write_text(glsl, encoding="utf-8")
        preview.write_bytes(png)
        record = directory / "candidate.parameters.json"
        if params is not None:
            params.save(record, code, preview, time=time)
            baked = save_baked_candidate(record, code, preview)
        output = CandidateRecord(
            id=f"{directory.name}-c001",
            task_id=source.task_id,
            code_path=str(code),
            preview_path=str(preview),
            render_record_path=str(record) if params is not None else None,
        )
    records = _publish_turning_points(directory, source, params, points, time) if params is not None else ()
    if records:
        diagnostics["turning_points"] = [
            {"candidate_id": record.id, "preview": Path(record.preview_path or "").name, "evaluation": point.evaluation, "error": point.error}
            for record, point in zip(records, points, strict=True)
        ]
    write_snapshot(directory / "revision.json", {**diagnostics, "source_candidate_id": source.id, "time": time})
    return NumericRevision(run_dir=directory, candidate=output, baked_code_path=baked, diagnostics=diagnostics, turning_points=records)


def run_numeric_revision(
    candidate: CandidateRecord,
    reference_path: Path,
    *,
    output_dir: Path,
    width: int,
    height: int,
    time: float,
    parameter_names: Sequence[str],
    regions: Sequence[PixelRegion] = (),
    anchors: Sequence[PixelRegion] = (),
    protected_regions: Sequence[ProtectedRegion] = (),
) -> NumericRevision:
    """锁定同一份 GLSL 和时刻, 仅修改白名单内已声明参数."""
    params = _candidate_params(candidate)
    if params is None or not parameter_names or candidate.render_record_path is None:
        msg = "定向优化必须指定已外置的参数白名单"
        raise ValueError(msg)
    recorded = json.loads(Path(candidate.render_record_path).read_text(encoding="utf-8"))
    if (
        candidate.preview_path is None
        or (width, height) != reference_size(Path(candidate.preview_path).read_bytes())
        or time != recorded["render"]["time"]
    ):
        msg = "定向优化必须使用候选已固定的画布与时间"
        raise ValueError(msg)
    glsl = Path(candidate.code_path).read_text(encoding="utf-8")
    directory = create_run_directory(output_dir)
    with WebGL2Renderer() as renderer:
        result = optimize_uniforms(
            renderer,
            glsl,
            reference_path.read_bytes(),
            width,
            height,
            time,
            specs=params.specs,
            uniforms=params.uniforms,
            parameter_names=tuple(parameter_names),
            regions=tuple(regions),
            anchors=tuple(anchors),
            protected_regions=tuple(protected_regions),
        )
    diagnostics: dict[str, JsonValue] = {
        "status": result.status,
        "start_error": result.start_error,
        "end_error": result.end_error,
        "best_observed_error": result.best_observed_error,
        "evaluations": result.evaluations,
        "boundary_parameters": list(result.boundary_parameters),
        "rejected_trials": result.rejected_trials,
    }
    adjusted = CandidateParameters(specs=params.specs, uniforms=result.uniforms)
    return _publish(directory, candidate, glsl, result.best_render, adjusted, diagnostics, time, result.turning_points)


def run_frame_revision(candidate: CandidateRecord, *, output_dir: Path, width: int, height: int, time: float) -> NumericRevision:
    """沿用 GLSL 和生效参数, 为人工选定的另一帧保存新候选."""
    params = _candidate_params(candidate)
    glsl = Path(candidate.code_path).read_text(encoding="utf-8")
    directory = create_run_directory(output_dir)
    with WebGL2Renderer() as renderer:
        png = renderer.render(glsl, width, height, time, uniforms=params.uniforms if params is not None else None)
    return _publish(directory, candidate, glsl, png, params, {"status": "rerendered"}, time)
