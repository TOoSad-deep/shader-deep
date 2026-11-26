"""渲染候选、保存代码与 PNG, 将成功或失败结果登记到黑板."""

# 本文件中的函数没有直接注册为模型 Tool; session.render_shader 负责包装和调度.
# 一次尝试分为三步: 保存源码 -> 调用真实渲染器 -> 将结果写回黑板并落盘.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_candidate, add_result
from shader_deep.domain.local_probe import validate_patch_fragment
from shader_deep.domain.shader_parameters import bake_uniforms, resolve_uniforms
from shader_deep.domain.tasks import CandidateRecord, ResultRecord
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters
from shader_deep.rendering import RenderError, WebGL2Renderer

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.agents.generation.tools.session import RenderSession
    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.shader_parameters import ParameterSpec


def _bind_code(glsl: str, probe: LocalProbeBinding | None) -> str:
    if probe is None or probe.baseline_glsl is None or probe.patch_source is None:
        return glsl
    validate_patch_fragment(probe.patch_source)
    validate_patch_fragment(glsl)
    return probe.baseline_glsl.replace(probe.patch_source, glsl, 1)


def render_candidate(session: RenderSession, glsl_code: str, specs: tuple[ParameterSpec, ...] = ()) -> str:
    """在会话的渲染线程中执行候选尝试并登记结果.

    Args:
        session: 持有预算、黑板和浏览器的当前会话.
        glsl_code: 待编译与渲染的完整 mainImage 代码.
        specs: 可选的外置参数声明, 首次渲染采用声明默认值.

    Returns:
        JSON 格式的候选状态、预览路径或错误和剩余预算.
    """
    if specs and not session.options.optimize_parameters:
        return json.dumps({"status": "invalid_parameters", "message": "本轮未启用外置参数"})
    rejection = session.begin_attempt()
    if rejection is not None:
        return rejection
    candidate_id, preview = candidate_paths(session)
    code_path = preview.with_suffix(".glsl")
    try:
        glsl_code = _bind_code(glsl_code, session.local_probe)
    except ValueError as exc:
        _save_code(session, candidate_id, code_path, glsl_code)
        return record_candidate(session, candidate_id, code_path, None, str(exc))
    _save_code(session, candidate_id, code_path, glsl_code)
    return _render_attempt(session, candidate_id, glsl_code, code_path, preview, specs)


def _save_code(session: RenderSession, candidate_id: str, code_path: Path, glsl_code: str) -> None:
    code_path.write_text(glsl_code, encoding="utf-8")
    if session.memory is not None:
        session.memory.register_code(candidate_id, code_path)


def _render_attempt(
    session: RenderSession, candidate_id: str, glsl_code: str, code_path: Path, preview: Path, specs: tuple[ParameterSpec, ...]
) -> str:
    try:
        uniforms = resolve_uniforms(specs)
        if session.options.optimize_parameters:
            bake_uniforms(glsl_code, specs, uniforms)
        params = CandidateParameters(specs=specs, uniforms=uniforms) if specs else None
        _render_png(session, glsl_code, preview, params)
    except (RenderError, ValueError) as exc:
        # 编译/渲染错误转成工具反馈, 让模型依据真实日志修正; 文件 I/O 错误仍会抛出.
        return record_candidate(session, candidate_id, code_path, None, str(exc))
    return record_candidate(session, candidate_id, code_path, preview, None, params=params)


def current_renderer(session: RenderSession) -> WebGL2Renderer:
    """在串行线程内按需启动浏览器, 由生成会话统一关闭."""
    if session.renderer is None:
        renderer = WebGL2Renderer()
        renderer.__enter__()
        session.renderer = renderer
    return session.renderer


def _render_png(session: RenderSession, glsl_code: str, preview: Path, params: CandidateParameters | None = None) -> None:
    renderer = current_renderer(session)
    args = (glsl_code, session.options.width, session.options.height, session.options.time)
    preview.write_bytes(renderer.render(*args) if params is None else renderer.render(*args, uniforms=params.uniforms))


def candidate_paths(session: RenderSession) -> tuple[str, Path]:
    """以本次尝试编号生成候选 ID 与预览路径."""
    return f"{session.run_dir.name}-c{session.attempts:03d}", session.run_dir / f"candidate-{session.attempts:03d}.png"


def record_candidate(
    session: RenderSession,
    candidate_id: str,
    code: Path,
    preview: Path | None,
    error: str | None,
    *,
    params: CandidateParameters | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    """登记已落盘或失败的候选, 由上下文负责呈现成功预览."""
    parameter_path = None
    if preview is not None and params is not None:
        parameter_path = preview.with_suffix(".parameters.json")
        params.save(parameter_path, code, preview, time=session.options.time)
    # CandidateRecord 记录产物及来源; ResultRecord 记录本次尝试发生了什么.
    # 两条记录都保留失败尝试, 便于后续上下文展示错误和用户追溯.
    candidate = CandidateRecord(
        id=candidate_id,
        task_id=session.task_id,
        code_path=str(code),
        preview_path=str(preview) if preview is not None else None,
        limitations=(error,) if error is not None else (),
        render_record_path=str(parameter_path) if parameter_path is not None else None,
    )
    # add_* 返回新黑板, 必须重新赋值; 先登记候选, 后登记引用该候选的结果.
    session.state = add_candidate(session.state, candidate)
    session.state = add_result(
        session.state,
        ResultRecord(
            id=f"{candidate_id}-render",
            task_id=session.task_id,
            # 渲染成功还没有完成视觉自检, 因而此处的业务提交仍为 partial.
            status="partial",
            candidate_ids=(candidate_id,),
            summary="真实 WebGL2 渲染成功, 等待对照参考检查" if error is None else "候选编译或渲染失败",
            limitations=(error,) if error is not None else (),
        ),
    )
    if preview is not None:
        session.successful.add(candidate_id)
    session.save()
    materials = session.memory.record_render(candidate_id, code, preview, error, parameter_path=parameter_path) if session.memory is not None else {}
    # 工具反馈是简短 JSON 字符串; 图片本体由下一轮 Context 读取, 不在这里内联.
    # 此处 status 描述渲染动作, 与 ResultRecord.status 的任务提交状态含义不同.
    return json.dumps(
        {
            "status": "rendered" if error is None else "render_error",
            "candidate_id": candidate_id,
            "preview_path": candidate.preview_path,
            "error": error,
            "attempts_remaining": session.options.max_attempts - session.attempts,
            **(extra or {}),
            **materials,
        },
        ensure_ascii=False,
    )
