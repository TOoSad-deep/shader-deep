"""使用独立历史执行一个分析视角, 并返回该任务的结果与执行记录."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain.tools import tool

from shader_deep.analysis.loop import AnalysisLoop
from shader_deep.analysis.prompts import LENS_PROMPT
from shader_deep.analysis.schemas import LensReport  # noqa: TC001  # 工具装饰器需要在运行时解析此参数结构.
from shader_deep.analysis.transport import configure_analysis_model
from shader_deep.analysis.types import AnalysisExecution, AnalysisLimitError, AnalysisOptions
from shader_deep.blackboard import add_result
from shader_deep.config import build_model
from shader_deep.context.analysis import build_analysis_context
from shader_deep.schemas import ResultRecord

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from shader_deep.schemas import BlackboardState


def run_lens(
    state: BlackboardState,
    task_id: str,
    reference_url: str,
    limit: int,
    config: RunnableConfig,
    *,
    options: AnalysisOptions | None = None,
    image_size: tuple[int, int] | None = None,
) -> tuple[ResultRecord, AnalysisExecution]:
    """在预算内执行独立分析任务, 并将失败保留为明确的结果记录.

    Args:
        state: 固定的任务快照; 工作线程不向共享黑板提交修改.
        task_id: 已登记的视角任务标识.
        reference_url: 包含参考 PNG 实际字节的数据 URL.
        limit: 模型调用次数上限, 包含修正报告的调用.
        config: 追踪元数据和图执行步数限制.
        options: 当前分析会话的网络传输与请求配置.
        image_size: 参考图原始尺寸, 用于提出测量建议.

    Returns:
        子任务结果, 以及由程序维护的执行记录.
    """
    # 列表作为提交工具和结束判定之间的共享槽位; 单个角色实例最多接受一份报告.
    # 它属于本次调用的局部状态, 不与其他视角共享聊天历史或提交状态.
    submitted: list[ResultRecord] = []
    execution = AnalysisExecution()
    options = options or AnalysisOptions()

    @tool
    def submit_analysis_report(report: LensReport, summary: str) -> str:
        """提交观察与解释, 并保证报告内的证据引用有效.

        Args:
            report: 结构化的独立视角报告.
            summary: 对分析结论及其局限的简要说明.
        """
        if submitted:
            return json.dumps({"status": "already_submitted", "result_id": submitted[0].id})
        if not summary.strip():
            return json.dumps({"status": "invalid_report", "message": "summary must not be blank"})
        result = ResultRecord(id=f"{task_id}-report", task_id=task_id, status="completed", summary=summary, analysis_detail=report)
        add_result(state, result)  # 针对输入快照校验; 只有协调器负责提交到共享黑板.
        submitted.append(result)
        return json.dumps({"status": "submitted", "result_id": result.id})

    loop = AnalysisLoop(
        execution,
        limit,
        lambda: build_analysis_context(
            state, task_id, reference_url, limits={"model_calls_remaining": limit - execution.model_calls}, image_size=image_size
        ),
        lambda: bool(submitted),
        [submit_analysis_report],
        request_retries=options.max_request_retries,
    )
    try:
        loop.run(configure_analysis_model(build_model(), options), LENS_PROMPT, config)
    except AnalysisLimitError as exc:
        execution.status, execution.error = "stopped", str(exc)
    except Exception as exc:  # noqa: BLE001  # 返回当前子任务的失败信息, 同时保留其他子任务报告.
        execution.status, execution.error = "failed", f"{type(exc).__name__}: {exc}"
    if submitted:
        execution.status = "completed"
        return submitted[0], execution
    # 即使没有可用报告也返回 blocked 记录, 主分析 Agent 可以据此保留缺口或安排补充.
    return ResultRecord(
        id=f"{task_id}-failure", task_id=task_id, status="blocked", summary=execution.error or "Worker did not submit a report"
    ), execution
