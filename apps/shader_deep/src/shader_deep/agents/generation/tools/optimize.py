"""在生成会话内锁定候选代码, 执行有界的数值搜索."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.tools.render import candidate_paths, current_renderer, record_candidate
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters, read_candidate_parameters
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.optimization import DEFAULT_OPTIMIZATION_EVALUATIONS, optimize_uniforms
from shader_deep.rendering import RenderError

if TYPE_CHECKING:
    from shader_deep.agents.generation.tools.session import RenderSession
    from shader_deep.domain.visual_review import PixelRegion
    from shader_deep.optimization import OptimizationResult, ProtectedRegion
    from shader_deep.optimization.uniforms import TurningPoint


def _optimization_details(result: OptimizationResult, baseline_id: str) -> dict[str, object]:
    return {
        "status": result.status,
        "baseline_candidate_id": baseline_id,
        "start_error": result.start_error,
        "end_error": result.end_error,
        "best_observed_error": result.best_observed_error,
        "evaluations": result.evaluations,
        "uniforms": result.uniforms,
        "boundary_parameters": result.boundary_parameters,
        "rejected_trials": result.rejected_trials,
    }


def optimize_candidate(
    session: RenderSession,
    baseline_id: str,
    parameter_names: tuple[str, ...] | None,
    regions: tuple[PixelRegion, ...],
    anchors: tuple[PixelRegion, ...] | None,
    protected_regions: tuple[ProtectedRegion, ...],
    *,
    max_evaluations: int = DEFAULT_OPTIMIZATION_EVALUATIONS,
) -> str:
    """对当前会话的参数化候选寻优, 登记关键拐点和胜出预览."""
    if not session.options.optimize_parameters or session.reference_png is None:
        return json.dumps({"status": "invalid_optimization", "message": "本轮未启用数值优化"})
    candidate = session.state["candidates"].get(baseline_id)
    if baseline_id not in session.successful or candidate is None or candidate.render_record_path is None or candidate.preview_path is None:
        return json.dumps({"status": "invalid_optimization", "message": "请选择本轮已成功渲染的参数化候选"})
    rejection = session.begin_attempt()
    if rejection is not None:
        return rejection
    code = Path(candidate.code_path)
    effective_anchors = session.reference_anchors if anchors is None else anchors
    try:
        params = read_candidate_parameters(Path(candidate.render_record_path), code, Path(candidate.preview_path))
        result = optimize_uniforms(
            current_renderer(session),
            code.read_text(encoding="utf-8"),
            session.reference_png,
            session.options.width,
            session.options.height,
            session.options.time,
            specs=params.specs,
            uniforms=params.uniforms,
            parameter_names=parameter_names,
            regions=regions,
            anchors=effective_anchors,
            protected_regions=protected_regions,
            max_evaluations=max_evaluations,
        )
    except (RenderError, ValueError) as exc:
        details: dict[str, object] = {"status": "optimization_error", "baseline_candidate_id": baseline_id, "message": str(exc)}
        return _record_optimization(session, details)
    details = _optimization_details(result, baseline_id)
    details["regions"] = [item.model_dump(mode="json") for item in regions]
    details["anchors"] = [item.model_dump(mode="json") for item in effective_anchors]
    details["protected_regions"] = [item.model_dump(mode="json") for item in protected_regions]
    if result.best_render is None:
        return _record_optimization(session, details)
    candidate_id, preview = candidate_paths(session)
    details["turning_points"] = [_record_turning_point(session, code, preview, candidate_id, params, point) for point in result.turning_points]
    details["candidate_id"] = candidate_id
    preview.write_bytes(result.best_render)
    if session.memory is not None:
        session.memory.register_code(candidate_id, code, operation="optimize_uniforms")
    answer = record_candidate(
        session,
        candidate_id,
        code,
        preview,
        None,
        params=CandidateParameters(specs=params.specs, uniforms=result.uniforms),
        extra={"optimization": details},
    )
    _record_optimization(session, details)
    return answer


def _record_turning_point(
    session: RenderSession, code: Path, preview: Path, candidate_id: str, params: CandidateParameters, point: TurningPoint
) -> dict[str, object]:
    point_id = f"{candidate_id}-e{point.evaluation}"
    point_path = preview.with_name(f"{preview.stem}-e{point.evaluation}.png")
    point_path.write_bytes(point.png)
    if session.memory is not None:
        session.memory.register_code(point_id, code, operation="optimize_uniforms")
    receipt = json.loads(
        record_candidate(session, point_id, code, point_path, None, params=CandidateParameters(specs=params.specs, uniforms=point.uniforms))
    )
    materials = {key: receipt[key] for key in ("code_material_id", "preview_material_id", "result_material_id") if key in receipt}
    return {"candidate_id": point_id, "evaluation": point.evaluation, "error": point.error, "uniforms": point.uniforms, **materials}


def _record_optimization(session: RenderSession, details: dict[str, object]) -> str:
    details["attempts_remaining"] = session.options.max_attempts - session.attempts
    path = session.run_dir / f"optimization-{session.attempts:03d}.json"
    write_snapshot(path, details, temporary_suffix=".json.tmp")
    session.save()
    return json.dumps(details, ensure_ascii=False)
