"""渲染候选、保存代码与 PNG, 将成功或失败结果登记到黑板."""

# 本文件中的函数没有直接注册为模型 Tool; session.render_shader 负责包装和调度.
# 一次尝试分为三步: 保存源码 -> 调用真实渲染器 -> 将结果写回黑板并落盘.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from shader_deep.domain.blackboard import add_candidate, add_result
from shader_deep.domain.tasks import CandidateRecord, ResultRecord
from shader_deep.rendering import RenderError, WebGL2Renderer

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.agents.generation.tools.session import RenderSession


def render_candidate(session: RenderSession, glsl_code: str) -> str:
    """在会话的渲染线程中执行候选尝试并登记结果.

    Args:
        session: 持有预算、黑板和浏览器的当前会话.
        glsl_code: 待编译与渲染的完整 mainImage 代码.

    Returns:
        JSON 格式的候选状态、预览路径或错误和剩余预算.
    """
    # 已选定候选后不再产生新版本; 超出预算的调用也不会增加次数或写新的源码.
    if session.selected is not None:
        return json.dumps({"status": "already_finished", "candidate_id": session.selected.id})
    if session.attempts >= session.options.max_attempts:
        session.save()
        return json.dumps({"status": "attempt_limit", "message": "渲染尝试预算已用完"})
    # 先计数再渲染, 所以编译失败也消耗一次预算.
    session.attempts += 1
    # 运行目录名区分不同会话, :03d 将序号补为 001、002 等便于排序的形式.
    candidate_id = f"{session.run_dir.name}-c{session.attempts:03d}"
    code_path = session.run_dir / f"candidate-{session.attempts:03d}.glsl"
    code_path.write_text(glsl_code, encoding="utf-8")
    # 先保存失败也能回看的源码; with_suffix 仅构造图片路径, 此时图片还不存在.
    preview = code_path.with_suffix(".png")
    try:
        _render_png(session, glsl_code, preview)
    except (RenderError, ValueError) as exc:
        # 编译/渲染错误转成工具反馈, 让模型依据真实日志修正; 文件 I/O 错误仍会抛出.
        return _record(session, candidate_id, code_path, None, str(exc))
    session.successful.add(candidate_id)
    # 渲染成功只代表有预览, 真正允许选择还需要下一轮 Context 将图片交给模型.
    return _record(session, candidate_id, code_path, preview, None)


def _render_png(session: RenderSession, glsl_code: str, preview: Path) -> None:
    # 浏览器按需启动一次并复用; 手动进入上下文, 由 RenderSession 退出时统一 close.
    if session.renderer is None:
        renderer = WebGL2Renderer()
        renderer.__enter__()
        session.renderer = renderer
    # 固定宽、高和 iTime 让不同候选在同样条件下比较; 返回值是 PNG 字节而非路径.
    png = session.renderer.render(glsl_code, session.options.width, session.options.height, session.options.time)
    preview.write_bytes(png)


def _record(session: RenderSession, candidate_id: str, code: Path, preview: Path | None, error: str | None) -> str:
    # CandidateRecord 记录产物及来源; ResultRecord 记录本次尝试发生了什么.
    # 两条记录都保留失败尝试, 便于后续上下文展示错误和用户追溯.
    candidate = CandidateRecord(
        id=candidate_id,
        task_id=session.task_id,
        code_path=str(code),
        preview_path=str(preview) if preview is not None else None,
        limitations=(error,) if error is not None else (),
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
    session.save()
    # 工具反馈是简短 JSON 字符串; 图片本体由下一轮 Context 读取, 不在这里内联.
    # 此处 status 描述渲染动作, 与 ResultRecord.status 的任务提交状态含义不同.
    return json.dumps(
        {
            "status": "rendered" if error is None else "render_error",
            "candidate_id": candidate_id,
            "preview_path": candidate.preview_path,
            "error": error,
            "attempts_remaining": session.options.max_attempts - session.attempts,
        },
        ensure_ascii=False,
    )
