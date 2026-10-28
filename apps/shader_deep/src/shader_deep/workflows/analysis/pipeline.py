"""原生主图只调用一次, 业务交付由实际合法提交决定."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from shader_deep.agents.analysis.native import analysis_input, build_analysis_agent
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.workflows.analysis.session import AnalysisSession

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.workflows.analysis.contracts import FiveAnalysisResult
    from shader_deep.workflows.options import AnalysisOptions


def execute_five_analysis(
    user_request: str,
    reference_url: str,
    options: AnalysisOptions,
    directory: Path,
    *,
    model: BaseChatModel | None = None,
    on_delivery: Callable[[FiveAnalysisResult, AnalysisSession], None] | None = None,
) -> FiveAnalysisResult:
    """执行一轮原生 task 图并保存实际产物, 不恢复或补问模型.

    Args:
        user_request: 已固定的用户要求.
        reference_url: 已固定原图的 PNG data URL.
        options: 本轮容量、各图限制与传输选项.
        directory: 本轮新建的独立运行目录.
        model: 可注入无网络模型; 缺省使用环境模型配置.
        on_delivery: 接受所选合法版本与业务记录的包写入回调.

    Returns:
        合法 V1、保留缺口的 V0 或没有合法产物的 failed.
    """
    session = AnalysisSession(user_request, reference_url, directory, options)
    selected_model = model if model is not None else build_model()
    log_analysis(directory, "原生分析运行开始", details={"user_request": user_request, "max_tasks": options.max_tasks})
    failure: str | None = None
    interrupted: KeyboardInterrupt | None = None
    try:
        graph = build_analysis_agent(selected_model, session, options)
        # SDK 承担主/子图调度及消息历史; 外层只启动一次, 不按最终文本追加补问.
        graph.invoke(analysis_input(session), config={"max_concurrency": options.max_parallel})
    except KeyboardInterrupt as error:
        interrupted = error
        failure = "分析运行被用户中断"
    except Exception as error:  # noqa: BLE001  # 图结束后仍按领域事实保存合法来源与部分结果.
        failure = f"{type(error).__name__}: {error}"
        log_analysis(directory, "原生分析图执行失败", level=logging.ERROR, details={"error": failure})
    # 正常返回、异常和用户中断都走业务核查, 已合法提交的来源不会随图失败丢弃.
    result = session.finalize(failure)
    if on_delivery is not None:
        on_delivery(result, session)
    log_analysis(directory, "原生分析运行结束", details={"status": result.status, "selected_version": result.selected_version})
    if interrupted is not None:
        # 保留部分交付后仍传播原中断, 让调用方维持取消语义.
        raise interrupted
    return result
