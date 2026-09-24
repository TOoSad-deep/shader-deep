"""旧报告协议的回归夹具; 仅测试调用旧会话, 不作为生产双路由或新协议验证."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.compatibility.analysis.prompts import MAIN_PROMPT
from shader_deep.compatibility.analysis.session import AnalysisSession
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.runtime.execution import AnalysisLimitError, AnalysisNoProgressError
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.workflows.outcomes import AnalysisOutcome


def raw_context_payload(request: dict[str, object]) -> dict[str, object]:
    for message in request["messages"]:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "text" and block["text"].startswith('{\n  "kind": "analysis_task_context"'):
                return json.loads(block["text"])
    msg = "Missing actual analysis context"
    raise AssertionError(msg)


def context_payload(request: dict[str, object]) -> dict[str, object]:
    # 模拟模型整合本轮材料和已收到的工具历史, 不修改实际 HTTP 请求或生产 Context Builder.
    payload = raw_context_payload(request)
    if payload["task"]["lens_config"] is None:
        read = {
            item["result_id"]: item["content"]
            for item in tool_results(request)
            if item.get("status") == "read" and item.get("complete") and item.get("pointer") == ""
        }
        payload["related_results"] = [read[item["result_id"]] for item in payload.get("report_files", []) if item["result_id"] in read]
        payload["related_results"].extend({**item, "analysis_detail": None} for item in payload.get("failed_results", []))
    return payload


class AnalysisFixture(GenerationFixture):
    """只用于旧 LensReport 协议回归, 不验证当前公开分析入口."""

    def response_message(self, request: dict[str, object]) -> dict[str, object]:
        payload = raw_context_payload(request)
        read = {
            item["result_id"] for item in tool_results(request) if item.get("status") == "read" and item.get("complete") and item.get("pointer") == ""
        }
        unread = [item for item in payload.get("report_files", []) if item["result_id"] not in read]
        if payload["task"]["lens_config"] is None and unread:
            message = {"role": "assistant", "content": "", "tool_calls": []}
            for index, report in enumerate(unread):
                call = self.call("read_analysis_file", {"file_path": report["file_path"], "pointer": ""})["tool_calls"][0]
                call["id"] += f"-read-{index}"
                message["tool_calls"].append(call)
            return message
        return self.response(request)


def run_legacy_analysis_task(
    state: BlackboardState, task_id: str, *, asset_root: Path | None = None, options: AnalysisOptions | None = None
) -> AnalysisOutcome:
    """仅为旧协议回归构造 AnalysisSession, 不调用当前公开入口.

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


def run_legacy_analysis(path: Path, prompt: str, *, options: AnalysisOptions | None = None) -> AnalysisOutcome:
    """仅供旧协议回归调用, 不暴露生产模式切换.

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
    return run_legacy_analysis_task(state, "A1", options=options)
