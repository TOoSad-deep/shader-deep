"""独立多视角分析入口, 通过视角配置驱动独立子任务."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.analysis.loop import AnalysisLoop
from shader_deep.analysis.prompts import MAIN_PROMPT
from shader_deep.analysis.session import AnalysisSession
from shader_deep.analysis.transport import configure_analysis_model
from shader_deep.analysis.types import AnalysisLimitError, AnalysisNoProgressError, AnalysisOptions, AnalysisOutcome
from shader_deep.artifacts import create_run_directory
from shader_deep.blackboard import add_target, add_task, new_blackboard
from shader_deep.config import build_model
from shader_deep.context.common import png_data_url
from shader_deep.schemas import TargetRecord, TaskRecord

if TYPE_CHECKING:
    from shader_deep.schemas import BlackboardState


def run_analysis_task(
    state: BlackboardState, task_id: str, *, asset_root: Path | None = None, options: AnalysisOptions | None = None
) -> AnalysisOutcome:
    """执行新的主分析任务, 不调用 Shader 生成流程.

    !!! warning "实验性接口"
        分析报告结构可能随视觉验收案例的补充而调整.

    Args:
        state: 已登记主分析任务的黑板.
        task_id: 尚未建立子任务的新主分析任务标识.
        asset_root: 相对输入路径的根目录; 未提供时使用当前目录.
        options: 任务数量、并发和模型调用预算.

    Returns:
        已保存的报告与综合结果, 或明确标记的未完成结果.

    Raises:
        ValueError: 任务、图片或模型配置无效.
        OSError: 输入或输出文件不可访问.
    """
    task = state["tasks"][task_id]
    if task.role != "analysis" or task.lens_config is not None or task.parent_task_id is not None or not task.objective.strip():
        msg = "Expected a root analysis task with a nonblank objective"
        raise ValueError(msg)
    if any(child.parent_task_id == task.id for child in state["tasks"].values()):
        msg = "Create a fresh root task for each analysis run"
        raise ValueError(msg)
    root = asset_root if asset_root is not None else Path.cwd()
    reference_url = png_data_url(root / state["targets"][task.target_version].reference_path)
    model = build_model()  # 创建输出目录前先校验模型配置.
    options = options or AnalysisOptions()
    model = configure_analysis_model(model, options)
    directory = create_run_directory(options.output_dir)
    # 启动时只读取一次原始图像, 后续主任务、子任务和测量都使用这份固定字节.
    # 即使调用方之后替换输入文件, 本次分析证据仍对应保存的 reference.png.
    (directory / "reference.png").write_bytes(base64.b64decode(reference_url.split(",", 1)[1]))
    session = AnalysisSession(state, task_id, options, directory, reference_url)
    loop = AnalysisLoop(
        session.execution,
        options.max_main_calls,
        session.context,
        lambda: session.summary_result is not None,
        session.tools(),
        request_retries=options.max_request_retries,
        submission_handler=session.submission_handler,
        on_prepared=session.on_prepared,
        on_event=session.event,
        max_repeated_no_progress=options.max_repeated_no_progress,
        progress=session.progress,
    )
    session.save()
    try:
        loop.run(model, MAIN_PROMPT, session.config(task_id))
    except AnalysisLimitError as exc:
        session.stop_reason, session.execution.status, session.execution.error = "model_limit", "stopped", str(exc)
    except AnalysisNoProgressError as exc:
        session.stop_reason, session.execution.status, session.execution.error = "no_progress", "stopped", str(exc)
    except Exception as exc:  # noqa: BLE001  # 模型服务或图执行失败时保留明确的未完成结果.
        session.stop_reason, session.execution.status, session.execution.error = "error", "failed", f"{type(exc).__name__}: {exc}"
    finally:
        # 正常结束、预算耗尽和执行失败都落盘, 调用方可从 outcome 定位已完成的部分.
        session.save()
        session.event("run_finished", {"status": session.stop_reason, "error": session.execution.error})
    return session.outcome()


def run_analysis(path: Path, prompt: str, *, options: AnalysisOptions | None = None) -> AnalysisOutcome:
    """通过预设或动态配置的分析视角处理本地 PNG.

    !!! warning "实验性接口"
        返回分析结论与假设, 不代表视觉验收通过, 也不生成 Shader 代码.

    Args:
        path: 本地 PNG 参考图路径.
        prompt: 用户要求与分析目标.
        options: 可选的程序执行预算.

    Returns:
        完整、部分完成或未完成的分析结果及运行目录.

    Raises:
        ValueError: 用户提示词或其他输入无效.
    """
    if not prompt.strip():
        msg = "Prompt must not be empty"
        raise ValueError(msg)
    state = add_target(new_blackboard(), TargetRecord(version="T1", request=prompt, reference_path=str(path.resolve())))
    state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective=prompt))
    return run_analysis_task(state, "A1", options=options)
