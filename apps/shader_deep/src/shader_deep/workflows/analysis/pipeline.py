"""单次调用原生观察图, 依据合法快照核查并交付."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING

from langchain_core.callbacks import UsageMetadataCallbackHandler

from shader_deep.agents.analysis.native import analysis_input, build_analysis_agent
from shader_deep.infrastructure.analysis_logging import log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.workflows.analysis.session import AnalysisSession

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain_core.language_models import BaseChatModel

    from shader_deep.workflows.analysis.contracts import SceneAnalysisResult
    from shader_deep.workflows.options import AnalysisOptions


def _diagnostics(session: AnalysisSession, result: SceneAnalysisResult) -> tuple[SceneAnalysisResult, OSError | None]:
    """独立尝试各诊断文件, 单个不可写不能阻止后续交付."""
    first: OSError | None = None
    for name in ("manifest.json", "result.json"):
        try:
            if name == "manifest.json":
                session.save_manifest()
            else:
                session.save_result(result)
        except OSError as error:
            first = first or error
            reason = f"diagnostic_error: {name}: {error}"
            result = replace(result, error=f"{result.error}; {reason}" if result.error else reason)
    return result, first


def _require_diagnostic(directory: Path, error: OSError | None) -> None:
    if error is not None and not (directory / "result.json").is_file():
        raise error


def _metrics(session: AnalysisSession, usage: UsageMetadataCallbackHandler, started: float) -> None:
    """只聚合 SDK 实际报告的 usage, 缺失时明确未知."""
    values = usage.usage_metadata
    totals = {key: sum(value[key] for value in values.values()) for key in ("input_tokens", "output_tokens", "total_tokens")} if values else None
    session.record_runtime(
        {
            "elapsed_seconds": perf_counter() - started,
            "elapsed_scope": "analysis_before_publication",
            "token_usage": totals,
            "usage_scope": "sdk_reported_responses_only",
            "http_request_count": None,
        }
    )


def execute_scene_analysis(
    user_request: str,
    reference_url: str,
    options: AnalysisOptions,
    directory: Path,
    *,
    model: BaseChatModel | None = None,
    on_delivery: Callable[[SceneAnalysisResult, AnalysisSession], SceneAnalysisResult | None] | None = None,
) -> SceneAnalysisResult:
    """运行一次观察图并交付已有合法结果, 中断保存后继续传播.

    Args:
        user_request: 固定观察目标与约束.
        reference_url: 固定原图 data URL.
        options: 已校验的运行配置.
        directory: 独立运行目录.
        model: 可注入的无网络测试模型.
        on_delivery: 发布和索引回调, 可返回包含发布失败的最终结果.

    Returns:
        实际合法观察或明确失败状态.
    """
    started = perf_counter()
    usage = UsageMetadataCallbackHandler()
    session = AnalysisSession(user_request, reference_url, directory, options)
    selected_model = model if model is not None else build_model()
    interrupted: KeyboardInterrupt | None = None
    failure: str | None = None
    try:
        graph = build_analysis_agent(selected_model, session, options)
        graph.invoke(analysis_input(session), config={"recursion_limit": options.max_model_calls * 4 + 10, "callbacks": [usage]})
    except KeyboardInterrupt as error:
        interrupted, failure = error, "分析运行被用户中断"
    except Exception as error:  # noqa: BLE001  # 图异常后仍核查已有接受快照.
        failure = f"{type(error).__name__}: {error}"
        with suppress(OSError):
            log_analysis(directory, "观察图执行失败", level=logging.ERROR, details={"error": failure})
    try:
        _metrics(session, usage, started)
        result = session.finalize(failure)
        result, diagnostic_error = _diagnostics(session, result)
        if on_delivery is not None:
            try:
                result = on_delivery(result, session) or result
            except OSError:
                # 必须先尝试发布/公开索引, 全盘失败时保留最早的原始 IO 错误.
                if diagnostic_error is not None:
                    raise diagnostic_error from None
                raise
        else:
            _require_diagnostic(directory, diagnostic_error)
    except Exception as error:  # 交付异常不能掩盖用户原始中断.
        if interrupted is None:
            raise
        # 磁盘已满等情况也可能导致日志写入失败, 原用户中断仍应优先传播.
        with suppress(OSError):
            log_analysis(directory, "中断后的交付失败", level=logging.ERROR, details={"error": f"{type(error).__name__}: {error}"})
    if interrupted is not None:
        raise interrupted
    return result
