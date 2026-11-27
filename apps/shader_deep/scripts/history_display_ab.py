"""冻结、串行执行及离线汇报历史展示 A/B; 真实调用必须显式选择 run/judge."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import shutil
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2
from deepagents import create_deep_agent
from langchain_core.messages import HumanMessage
from langchain_core.tracers.langchain import LangChainTracer
from langchain_openai import ChatOpenAI
from langsmith import Client

from scripts._history_ab_evidence import Evidence, now, safe, sha256, write_json
from scripts._history_ab_policy import ExperimentTools
from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_run import run_parameter_task
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.experiments.tools import build_parameter_tools
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.rendering import ShaderPreview

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool

    from shader_deep.experiments.parameter_evaluation import ReferenceSpec
    from shader_deep.experiments.parameter_run import ParameterRunResult
    from shader_deep.rendering import ParameterSpec, PreviewSnapshot

APP = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = APP / "runs/run-circles-case-5686vir4"
DEFAULT_ENV = Path("/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep/.env")
JUDGE_OUTPUT_LIMIT = 16384
JUDGE_TIMEOUT = 90
REQUIREMENT = (
    "只调整工具允许的参数, 保持源码、参数范围、参考图和渲染条件固定. "
    "本次总预算是18次实际模型请求尝试, 包括失败和重试, 不是18次参数更新. "
    "每次参数更新后单独调用 compare_preview 检查实际变化, 基线也使用 compare_preview. "
    "当无法判断是否改善、发现退步、或考虑恢复以前参数时, 可按需调用 get_trial_history "
    "回看过去的参数与图片, 再决定继续、恢复或停止; 不要求每轮读取历史. "
    "历史图片只提供证据, 不提供评分、排名或自动恢复. "
    "先处理轮廓、大小、位置, 再处理颜色、亮度与光晕; 只依据实际图片判断. "
    "参数含义: u_scale=整体缩放; u_bloom=前景圆内重叠区域的紫色晕染强度; "
    "u_pink=前景圆顶部粉色染色强度; u_rim=重叠边缘描边强度; "
    "u_blur=前景圆内后方圆边界的模糊宽度. "
    "结束前必须依据最后一次成功更新后的最新图片复查主要差异, "
    "报告最终参数、对应revision及尚未匹配或无法判断之处. 不调用其他工具或子任务."
)


def load_json(path: Path) -> dict:
    """读取本地实验对象."""
    return json.loads(path.read_text())


def load_environment(path: Path) -> None:
    """读取已有 dotenv 简单键值, 不打印或保存凭据."""
    for line in path.read_text().splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        name, value = item.removeprefix("export ").split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_PROJECT"] = "shader-deep"


def configured_model() -> ChatOpenAI:
    """校验实验目标模型, 工程子 Agent 的模型不参与此选择."""
    model = build_model()
    if model.model_name != "z-ai/glm-5.3-flash" or urlsplit(str(model.openai_api_base)).hostname != "openrouter.ai":
        msg = "Experiment requires existing OpenRouter z-ai/glm-5.3-flash configuration"
        raise ValueError(msg)
    body = dict(model.extra_body or {})
    body["provider"] = {"require_parameters": True, "only": ["coreweave/nvfp4"], "allow_fallbacks": False}
    model.extra_body = body
    return model


def source_paths() -> list[Path]:
    """限定冻结当前实验、渲染器和脚本, 不读取未知文件或秘密文件."""
    files = [
        path
        for folder in ("experiments", "rendering", "imaging")
        for path in (APP / "src/shader_deep" / folder).rglob("*")
        if path.suffix in {".py", ".js", ".html"}
    ]
    return sorted([*files, Path(__file__), APP / "scripts/_history_ab_evidence.py", APP / "scripts/_history_ab_policy.py"])


def prepare(directory: Path, source: Path, env_file: Path) -> None:
    """冻结六次尝试的输入、提示、实现、评审规则和顺序, 不请求模型."""
    from shader_deep.experiments.parameter_evaluation import (  # noqa: PLC0415  # 离线帮助不加载评估模块.  # noqa: PLC0415  # 冻结独立评估协议.
        PROTOCOL,
        REGIONS,
        build_judge_request,
        freeze_reference,
    )

    load_environment(env_file)
    configured = configured_model()
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("adapted.glsl", "reference.png", "manifest.json"):
        shutil.copyfile(source / name, directory / name)
    task = build_reference_task(directory / "reference.png", requirement=REQUIREMENT)
    text = cast("list[dict]", task.content)[0]["text"]
    (directory / "task.txt").write_text(text)
    hashes = {}
    for path in source_paths():
        name = str(path.relative_to(APP))
        hashes[name] = sha256(path.read_bytes())
        target = directory / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    spec = freeze_reference(directory / "reference.png")
    judge_request = build_judge_request(spec, directory / "reference.png")
    inventory = Path("/private/tmp/shader-history-provider-inventory-20261106.json")
    if inventory.is_file():
        shutil.copyfile(inventory, directory / "provider-inventory.json")
    cases = [
        {"id": f"R{repeat}-{group}", "repeat": repeat, "group": group, "history_mode": "original" if group == "A" else "paired"}
        for repeat, order in ((1, "AB"), (2, "BA"), (3, "AB"))
        for group in order
    ]
    suite = {
        "status": "frozen",
        "created_at": now(),
        "cases": cases,
        "source_sha256": hashes,
        "task_sha256": sha256(text.encode()),
        "reference_spec": {"sha256": spec.sha256, "source": spec.source},
        "evaluation_protocol": {
            "version": PROTOCOL,
            "execution_mode": "native_json_schema_v3",
            "response_format_sha256": sha256(json.dumps(judge_request["response_format"], sort_keys=True).encode()),
            "regions": REGIONS,
            "judge_schema": judge_request["schema"],
            "judge_prompt": cast("list[dict]", judge_request["messages"])[0]["content"][0]["text"],
        },
        "reference_sha256": sha256((directory / "reference.png").read_bytes()),
        "shader_sha256": sha256((directory / "adapted.glsl").read_bytes()),
        "manifest_sha256": sha256((directory / "manifest.json").read_bytes()),
        "limits": {
            "actual_request_attempts": 18,
            "output_tokens": 8192,
            "timeout_seconds": 45,
            "sdk_retries": 0,
            "connection_retries": 1,
            "graph_steps": 160,
        },
        "model": "z-ai/glm-5.3-flash",
        "model_extra_body": configured.extra_body,
        "project": "shader-deep",
        "provider": {"require_parameters": True, "only": ["coreweave/nvfp4"], "allow_fallbacks": False},
        "interpretation": "Presentation package effect: paired layout repeats the reference; not a pure layout-only causal contrast. "
        "Small stochastic study, provider pinned to coreweave/nvfp4 without fallback, failures retained, no replacement runs. "
        "Numeric diagnostics and independent uncalibrated judges do not establish human visual acceptance.",
        "judge_budget": {
            "attempts_per_artifact": 1,
            "controls": ["reference_self", "initial"],
            "separate_from_tuning": True,
            "output_tokens": JUDGE_OUTPUT_LIMIT,
            "timeout_seconds": JUDGE_TIMEOUT,
            "sdk_retries": 0,
        },
        "numeric_selection_rule": "minimum foreground normalized RGB MAE over successful snapshots; earliest snapshot breaks ties; not visual-best",
    }
    write_json(directory / "suite.json", suite)
    print(directory, flush=True)  # noqa: T201  # CLI 输出可审查的制品路径.


def validate_freeze(directory: Path, suite: dict) -> None:
    """运行前拒绝输入或被冻结文件漂移; 后续新增的无关文件不影响此集合."""
    for name, expected in suite["source_sha256"].items():
        if sha256((APP / name).read_bytes()) != expected:
            msg = f"Frozen source changed: {name}"
            raise RuntimeError(msg)
    for name, key in (
        ("reference.png", "reference_sha256"),
        ("adapted.glsl", "shader_sha256"),
        ("manifest.json", "manifest_sha256"),
        ("task.txt", "task_sha256"),
    ):
        if sha256((directory / name).read_bytes()) != suite[key]:
            msg = f"Frozen input changed: {name}"
            raise RuntimeError(msg)


class RecordedPreview:
    """委托已有预览并保留每次截图/更新, 不额外渲染或在错误后取图."""

    def __init__(self, preview: ShaderPreview, evidence: Evidence) -> None:
        """绑定同一次实验的浏览器与证据流."""
        self.preview = preview
        self.evidence = evidence

    @property
    def parameter_specs(self) -> dict[str, ParameterSpec]:
        """沿用浏览器管理的参数白名单."""
        return self.preview.parameter_specs

    def set_parameters(self, parameters: dict[str, float]) -> dict[str, float]:
        """只登记成功应用的参数, 单调事件序号用于末次修改的新鲜度判断."""
        result = self.preview.set_parameters(parameters)
        entry: dict[str, object] = {
            "number": len(self.evidence.updates) + 1,
            "parameters": result,
            "at": now(),
            "http_attempt": len(self.evidence.http_requests),
        }
        self.evidence.updates.append(entry)
        self.evidence.event("parameter_update", **entry)
        return result

    def capture(self) -> PreviewSnapshot:
        """保留原始 PNG 与 revision, 包括重复参数和正常收尾截图."""
        snapshot = self.preview.capture()
        filename = f"snapshot-{len(self.evidence.snapshots):02d}.png"
        (self.evidence.directory / filename).write_bytes(snapshot.png)
        entry: dict[str, object] = {
            "file": filename,
            "sha256": sha256(snapshot.png),
            "revision": snapshot.revision,
            "parameters": dict(snapshot.parameters),
            "width": snapshot.width,
            "height": snapshot.height,
            "time": snapshot.time,
            "update_number": len(self.evidence.updates),
            "http_attempt": len(self.evidence.http_requests),
        }
        self.evidence.snapshots.append(entry)
        self.evidence.event("snapshot", **entry)
        return snapshot


def model_with_transport(model: ChatOpenAI, transport: httpx2.Client, *, output_limit: int = 8192, timeout: int = 45) -> ChatOpenAI:
    """显式关闭 SDK 重试并沿用提供方设置, 实际请求由 hooks 单独计数."""
    return ChatOpenAI(
        model=model.model_name,
        api_key=model.openai_api_key,
        base_url=model.openai_api_base,
        extra_body=model.extra_body,
        http_client=transport,
        use_responses_api=False,
        max_tokens=output_limit,
        max_retries=0,
        timeout=timeout,
    )


def execute_case(directory: Path, suite: dict, case: dict, configured: ChatOpenAI, client: Client) -> None:
    """执行一次独立图/历史/浏览器, 失败保留原始错误且绝不补跑."""
    validate_freeze(directory, suite)
    target = directory / case["id"]
    target.mkdir(exist_ok=False)
    evidence = Evidence(target, suite["reference_sha256"])
    record = {**case, "status": "running", "started_at": now(), "trace": {"run_id": str(uuid4()), "project": "shader-deep"}}
    write_json(target / "summary.json", record)
    started = time.monotonic()
    try:
        manifest = load_json(directory / "manifest.json")
        with httpx2.Client(
            event_hooks={"request": [evidence.request], "response": [evidence.response]}, timeout=45, follow_redirects=False
        ) as transport:
            model = model_with_transport(configured, transport)
            with ShaderPreview(
                (directory / "adapted.glsl").read_text(),
                parameters=manifest["parameters"],
                width=manifest["width"],
                height=manifest["height"],
                time=manifest["time"],
                headless=True,
            ) as browser:
                preview = RecordedPreview(browser, evidence)
                preview.capture()

                def selected_tools(
                    bound: ShaderPreview, *, reference_path: Path | None = None, history_mode: Literal["original", "paired"] = "original"
                ) -> list[BaseTool]:
                    return [
                        tool
                        for tool in build_parameter_tools(bound, reference_path=reference_path, history_mode=history_mode)
                        if tool.name != "capture_preview"
                    ]

                def restricted_agent(**options: object) -> object:
                    options["middleware"] = [ExperimentTools(), *cast("list", options["middleware"])]
                    factory = cast("Callable[..., object]", create_deep_agent)
                    return factory(**options)

                with (
                    patch("shader_deep.experiments.parameter_agent.build_parameter_tools", selected_tools),
                    patch("shader_deep.experiments.parameter_agent.create_deep_agent", restricted_agent),
                ):
                    agent = build_parameter_agent(
                        preview=cast("ShaderPreview", preview),
                        model=model,
                        reference_path=directory / "reference.png",
                        history_mode=case["history_mode"],
                    )
                task = HumanMessage(
                    content=[
                        {"type": "text", "text": (directory / "task.txt").read_text()},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + base64.b64encode((directory / "reference.png").read_bytes()).decode()},
                        },
                    ]
                )
                runner = cast("Callable[..., ParameterRunResult]", run_parameter_task)
                result = runner(
                    agent,
                    task,
                    cast("ShaderPreview", preview),
                    max_model_calls=18,
                    config={
                        "recursion_limit": 160,
                        "callbacks": [evidence],
                        "run_name": f"history-display.{case['id']}",
                        "metadata": {"study": directory.name, **case},
                        "tags": ["history-display-ab", case["id"]],
                    },
                    langsmith_extra={
                        "run_id": UUID(record["trace"]["run_id"]),
                        "client": client,
                        "project_name": "shader-deep",
                        "metadata": {"study": directory.name, **case},
                    },
                )
                record.update(
                    status="completed",
                    stop_reason="normal_final_response",
                    model_calls=result.model_calls,
                    final_reply=safe(result.messages[-1].content),
                    final_snapshot=evidence.snapshots[-1],
                )
    except Exception as error:  # noqa: BLE001  # 每个已授权尝试的原始失败必须保留, 不替换或重跑.
        record.update(
            status="failed",
            stop_reason=type(error).__name__,
            error={"type": type(error).__name__, "message": safe(error), "notes": [safe(note) for note in getattr(error, "__notes__", [])]},
        )
    record.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - started, 3), **evidence.summary())
    write_json(target / "summary.json", record)
    case.update(status=record["status"], stop_reason=record["stop_reason"])
    write_json(directory / "suite.json", suite)
    print(case["id"], record["status"], "actual requests", record["http_attempts"], flush=True)  # noqa: T201  # 显示实验进度与硬预算计数.


def run(directory: Path, env_file: Path) -> None:
    """六次串行尝试; 中断后仅跳过已开始的目录, 绝不替换失败样本."""
    load_environment(env_file)
    suite = load_json(directory / "suite.json")
    validate_freeze(directory, suite)
    if not (directory / "judges/controls-summary.json").is_file():
        msg = "Run judge-controls before tuning so failed sanity controls remain visible"
        raise RuntimeError(msg)
    configured = configured_model()
    if configured.extra_body != suite["model_extra_body"]:
        msg = "Model reasoning/provider configuration differs from frozen experiment"
        raise RuntimeError(msg)
    suite.update(status="running", started_at=now())
    write_json(directory / "suite.json", suite)
    with closing(Client(timeout_ms=(10000, 20000))) as client:
        for case in suite["cases"]:
            if (directory / case["id"]).exists():
                continue
            execute_case(directory, suite, case, configured, client)
            client.flush(timeout=15)
    suite.update(status="finished", ended_at=now())
    write_json(directory / "suite.json", suite)
    report(directory)


def evaluate_case(directory: Path, record: dict, spec: ReferenceSpec) -> None:
    """离线计算所有快照并检查最后更新后同版本图片是否进入模型请求."""
    from shader_deep.experiments.parameter_evaluation import evaluate_render  # noqa: PLC0415  # 评估只在离线报告阶段发生.

    for snapshot in record.get("snapshots", []):
        if sha256((directory / snapshot["file"]).read_bytes()) != snapshot["sha256"]:
            msg = f"Recorded snapshot bytes changed: {snapshot['file']}"
            raise RuntimeError(msg)
        snapshot["metrics"] = evaluate_render(spec, directory / snapshot["file"])
    final = record.get("final_snapshot")
    tools = record.get("tool_results", [])
    final_update = len(record.get("successful_updates", []))
    delivered = []
    for request in record.get("http_requests", []):
        for image in request["images"]:
            metadata = image.get("metadata")
            if not isinstance(metadata, dict) or final is None:
                continue
            if image["role"] != "tool" or metadata.get("revision") != final["revision"] or metadata.get("parameters") != final["parameters"]:
                continue
            receipts = [
                tool
                for tool in tools
                if tool.get("status") == "success"
                and tool.get("name") == "compare_preview"
                and tool.get("http_attempt", 0) < request["number"]
                and any(item["sha256"] == image["sha256"] for item in tool.get("images", []))
            ]
            matching_snapshots = [
                snapshot
                for snapshot in record["snapshots"]
                if snapshot["revision"] == final["revision"]
                and snapshot["parameters"] == final["parameters"]
                and snapshot["sha256"] == final["sha256"]
                and snapshot["update_number"] == final_update
                and snapshot["http_attempt"] < request["number"]
            ]
            if receipts and matching_snapshots:
                delivered.append(request["number"])
    record["delivery"] = {
        "final_image_in_later_model_request": bool(delivered),
        "request_numbers": delivered,
        "basis": "same final revision, full parameters, raw PNG hash; exact comparison bytes in later actual request after last successful update",
    }
    record["counts"] = {
        "history_calls": sum(tool.get("name") == "get_trial_history" for tool in tools),
        "successful_updates": final_update,
        "snapshots": len(record.get("snapshots", [])),
    }
    snapshots = record.get("snapshots", [])
    if snapshots:
        record["best_numeric"] = min(snapshots, key=lambda item: item["metrics"]["regions"]["foreground"]["mae"])
        record["numeric_selection_rule"] = (
            "minimum foreground normalized RGB MAE over successful snapshots; earliest snapshot breaks ties; not visual-best"
        )
        if final:
            final["metrics"] = next(snapshot["metrics"] for snapshot in snapshots if snapshot["file"] == final["file"])


def usage_summary(attempts: list[dict]) -> dict:
    """统计已报告成本及缺失覆盖, 未知请求不按零成本处理."""
    reported = [attempt["usage"] for attempt in attempts if isinstance(attempt.get("usage"), dict)]
    costs = [usage["cost"] for usage in reported if isinstance(usage.get("cost"), (float, int))]
    return {
        "actual_attempts": len(attempts),
        "usage_reported_attempts": len(reported),
        "usage_missing_attempts": len(attempts) - len(reported),
        "reported_total_tokens": sum(usage.get("total_tokens", 0) for usage in reported),
        "reported_cost_usd": sum(costs) if costs else None,
        "cost_reported_attempts": len(costs),
    }


def judge_one(directory: Path, image: Path, identifier: str, configured: ChatOpenAI, client: Client, *, budget: dict) -> dict:
    """独立单次盲评, 不提供实验组、轨迹、参数或数值评分; 失败不重试."""
    from shader_deep.experiments.parameter_evaluation import (  # noqa: PLC0415  # 调参与事后评审入口隔离.
        build_judge_request,
        freeze_reference,
        parse_judge_result,
    )

    folder = directory / "judges" / identifier
    folder.mkdir(parents=True, exist_ok=False)
    spec = freeze_reference(directory / "reference.png")
    request = build_judge_request(spec, image)
    # 保留可审查的盲评材料, 路径和 identifier 只在外层记录中使用.
    write_json(folder / "request.json", request)
    record: dict = {
        "id": identifier,
        "status": "running",
        "started_at": now(),
        "image": str(image.relative_to(directory)),
        "pair_id": request["pair_id"],
        "requests": [],
        "trace_id": str(uuid4()),
        "separate_tuning_budget": True,
        "budget": budget,
    }
    write_json(folder / "summary.json", record)

    def on_request(http_request: httpx2.Request) -> None:
        if record["requests"]:
            msg = "Judge one-attempt limit reached"
            raise RuntimeError(msg)
        data = json.loads(http_request.content)
        if (
            http_request.url.host != "openrouter.ai"
            or "tools" in data
            or data.get("messages") != request["messages"]
            or data.get("response_format") != request["response_format"]
        ):
            msg = "Blind judge payload does not match frozen reference/render request"
            raise RuntimeError(msg)
        output_limit = data.get("max_completion_tokens", data.get("max_tokens"))
        if output_limit != budget["output_tokens"]:
            msg = "Judge output limit differs from frozen budget"
            raise RuntimeError(msg)
        timeout_policy = http_request.extensions.get("timeout")
        if not isinstance(timeout_policy, dict) or any(value != budget["timeout_seconds"] for value in timeout_policy.values()):
            msg = "Judge HTTP timeout differs from frozen budget"
            raise RuntimeError(msg)
        record["requests"].append(
            {
                "number": 1,
                "at": now(),
                "model": data["model"],
                "provider_policy": data.get("provider"),
                "output_limit": output_limit,
                "timeout_seconds": timeout_policy,
                "response_format": data["response_format"],
            }
        )
        write_json(folder / "summary.json", record)

    def on_response(response: httpx2.Response) -> None:
        entry = record["requests"][-1]
        entry["http_status"] = response.status_code
        if "json" in response.headers.get("content-type", ""):
            body = json.loads(response.read())
            entry.update(provider=body.get("provider"), usage=body.get("usage"), response_id=body.get("id"))
        write_json(folder / "summary.json", record)

    try:
        with httpx2.Client(
            event_hooks={"request": [on_request], "response": [on_response]}, timeout=budget["timeout_seconds"], follow_redirects=False
        ) as transport:
            model = model_with_transport(configured, transport, output_limit=budget["output_tokens"], timeout=budget["timeout_seconds"])
            message = model.bind(response_format=request["response_format"]).invoke(
                [HumanMessage(content=cast("list[dict]", request["messages"])[0]["content"])],
                config={
                    "run_id": UUID(record["trace_id"]),
                    "run_name": "history-display.independent-judge",
                    "metadata": {"study": directory.name, "pair_id": request["pair_id"]},
                    "tags": ["independent-judge"],
                    "callbacks": [LangChainTracer(project_name="shader-deep", client=client)],
                },
            )
            raw = safe(message.content)
            (folder / "raw.txt").write_text(raw)
            verdict = parse_judge_result(raw, str(request["pair_id"]))
            if message.response_metadata.get("finish_reason") != "stop":
                verdict = {"overall": "unassessed", "parse_error": "Model did not finish normally", "acceptance": "uncalibrated"}
            record.update(status="completed", verdict=verdict, response_metadata=message.response_metadata, usage=message.usage_metadata)
    except Exception as error:  # noqa: BLE001  # 单次独立评审失败保留, 不猜补结论或替换样本.
        record.update(
            status="failed",
            error={"type": type(error).__name__, "message": safe(error)},
            verdict={"overall": "unassessed", "acceptance": "uncalibrated"},
        )
    record["ended_at"] = now()
    write_json(folder / "summary.json", record)
    client.flush(timeout=15)
    return record


def judge(directory: Path, env_file: Path, *, controls: bool) -> None:
    """先检查参考自比/初始图控制, 或对全部正常最终图独立评审一次."""
    load_environment(env_file)
    suite = load_json(directory / "suite.json")
    validate_freeze(directory, suite)
    budget = suite["judge_budget"]
    expected = {"output_tokens": JUDGE_OUTPUT_LIMIT, "timeout_seconds": JUDGE_TIMEOUT, "sdk_retries": 0, "attempts_per_artifact": 1}
    if any(budget.get(name) != value for name, value in expected.items()):
        msg = "Independent judge budget differs from frozen preflight v2 configuration"
        raise RuntimeError(msg)
    configured = configured_model()
    if configured.extra_body != suite["model_extra_body"]:
        msg = "Judge model configuration differs from frozen experiment"
        raise RuntimeError(msg)
    artifacts = []
    if controls:
        manifest = load_json(directory / "manifest.json")
        initial = directory / "initial-control.png"
        with ShaderPreview(
            (directory / "adapted.glsl").read_text(),
            parameters=manifest["parameters"],
            width=manifest["width"],
            height=manifest["height"],
            time=manifest["time"],
            headless=True,
        ) as preview:
            initial.write_bytes(preview.capture().png)
        artifacts = [("reference-self", directory / "reference.png"), ("initial-control", initial)]
    else:
        for case in suite["cases"]:
            path = directory / case["id"] / "summary.json"
            if path.exists():
                record = load_json(path)
                if record.get("status") == "completed":
                    final = record["final_snapshot"]
                    image = path.parent / final["file"]
                    if sha256(image.read_bytes()) != final["sha256"]:
                        msg = f"Final judge artifact changed: {case['id']}"
                        raise RuntimeError(msg)
                    artifacts.append((f"sample-{len(artifacts) + 1:02d}", image))
    records = []
    with closing(Client(timeout_ms=(10000, 20000))) as client:
        for identifier, image in artifacts:
            path = directory / "judges" / identifier / "summary.json"
            records.append(load_json(path) if path.exists() else judge_one(directory, image, identifier, configured, client, budget=budget))
    write_json(directory / "judges" / ("controls-summary.json" if controls else "finals-summary.json"), records)
    report(directory)


def report(directory: Path) -> None:
    """生成可链接的离线 HTML, 展示实际末图与按固定数值指标选出的图."""
    from shader_deep.experiments.parameter_evaluation import freeze_reference  # noqa: PLC0415  # 不在调参循环导入评分.

    suite = load_json(directory / "suite.json")
    validate_freeze(directory, suite)
    spec = freeze_reference(directory / "reference.png")
    panels = []
    for case in suite["cases"]:
        path = directory / case["id"] / "summary.json"
        if not path.exists():
            continue
        record = load_json(path)
        evaluate_case(path.parent, record, spec)
        write_json(path, record)
        pictures = ['<figure><img src="reference.png"><figcaption>固定参考</figcaption></figure>']
        snapshots = record.get("snapshots", [])
        if snapshots:
            pictures.append(f'<figure><img src="{case["id"]}/{snapshots[0]["file"]}"><figcaption>初始</figcaption></figure>')
        final = record.get("final_snapshot")
        if final:
            pictures.append(f'<figure><img src="{case["id"]}/{final["file"]}"><figcaption>实际最终图</figcaption></figure>')
        else:
            pictures.append("<p>本次失败, 无正常最终交付. 已捕获的快照保留在运行目录.</p>")
        best = record.get("best_numeric")
        if best:
            pictures.append(f'<figure><img src="{case["id"]}/{best["file"]}"><figcaption>数值最优: 前景 MAE 最小, 非视觉最优</figcaption></figure>')
        summary = {key: record.get(key) for key in ("status", "stop_reason", "http_attempts", "counts", "delivery", "trace", "error")}
        summary["usage"] = usage_summary(record.get("http_requests", []))
        summary["final_metrics"] = next((item["metrics"] for item in snapshots if final and item["file"] == final["file"]), None)
        summary["best_numeric_metrics"] = best.get("metrics") if best else None
        details = html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
        panels.append(
            f"<section><h2>{html.escape(case['id'])} · {html.escape(case['history_mode'])}</h2>"
            f'<div class="images">{"".join(pictures)}</div><pre>{details}</pre>'
            f'<p><a href="{case["id"]}/summary.json">全部快照、数值指标、请求、用量与提供方</a></p></section>'
        )
    document = (
        '<!doctype html><meta charset="utf-8"><title>历史展示 A/B</title><style>'
        "body{font:16px system-ui;max-width:1500px;margin:32px auto;padding:0 24px;background:#fafafa;color:#222}"
        ".images{display:flex;gap:12px;flex-wrap:wrap}figure{margin:0;flex:1;min-width:280px}img{width:100%;height:auto}"
        "section{border-top:1px solid #bbb;padding:24px 0}pre{white-space:pre-wrap;font-size:13px}</style>"
        "<h1>历史展示 A/B · 三对串行尝试</h1><p>每次18次实际请求硬上限, 重试计入. "
        "两组均有 compare_preview 和可选历史读取. 最终图、数值最优图与用户视觉验收分开.</p>"
    )
    document += f"<p>{html.escape(suite['interpretation'])}</p>" + "".join(panels)
    document += "<section><h2>独立盲评及控制 · 不校准为用户验收</h2>"
    for path in sorted((directory / "judges").glob("*/summary.json")):
        result = load_json(path)
        document += f'<p><a href="{path.relative_to(directory)}">独立评审完整回执: {html.escape(path.parent.name)}</a></p>'
        result.pop("response_metadata", None)
        result["cost_coverage"] = usage_summary(result.pop("requests", []))
        document += f"<pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre>"
    document += "</section>"
    (directory / "report.html").write_text(document)
    print(directory / "report.html", flush=True)  # noqa: T201  # CLI 返回本地制品路径, 不自动打开浏览器.


def main() -> None:
    """执行显式选择的阶段; prepare/report 不产生模型请求."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report", "judge-controls", "judge-finals"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        prepare(arguments.directory.resolve(), arguments.source.resolve(), arguments.env_file)
    elif arguments.command == "run":
        run(arguments.directory.resolve(), arguments.env_file)
    elif arguments.command.startswith("judge-"):
        judge(arguments.directory.resolve(), arguments.env_file, controls=arguments.command == "judge-controls")
    else:
        report(arguments.directory.resolve())


if __name__ == "__main__":
    main()
