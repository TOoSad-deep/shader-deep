"""一次性框架评估: 只在独立进程替换探索装配, 不改默认实现."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import signal
import time
import unittest
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from statistics import median
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.tools import tool
from langchain_core.messages import HumanMessage

from shader_deep.agents.analysis import execution as assembly
from shader_deep.agents.analysis.exploration import run_exploration
from shader_deep.domain.five_libraries import Element, ExplorationSubmission
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.transport import AnalysisChatOpenAI
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.five_analysis_loop import FiveAnalysisLoop
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.options import AnalysisOptions

ROOT = Path(__file__).resolve().parent
BASE = assembly.assemble_role
OPTIONS = AnalysisOptions(max_worker_calls=2, max_request_retries=0, max_output_tokens=8192, request_timeout_seconds=90)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def request_summary(payload):
    messages, tools = payload.get("messages", []), payload.get("tools", [])
    return {"payload_sha256": digest(payload), "messages_sha256": digest(messages),
            "tools_sha256": digest(tools), "payload_bytes": len(json.dumps(payload).encode()),
            "tool_names": [entry.get("function", {}).get("name") for entry in tools],
            "image_blocks": sum(block.get("type") == "image_url" for msg in messages
                                for block in (msg.get("content") or []) if isinstance(block, dict))}


def adapter(model, prompt, loop, *, skill=None):
    if skill is not None or "submit_exploration" not in {item.name for item in loop.tools}:
        return BASE(model, prompt, loop, skill=skill)
    loop.role_prompt = prompt
    return create_agent(model=model, tools=loop.tools, system_prompt=prompt,
                        middleware=[SummarizationMiddleware(model=model, trigger=None), loop])


@contextmanager
def selected(condition):
    with patch.object(assembly, "assemble_role", adapter if condition == "create_agent" else BASE):
        yield


def source_versions():
    return {name: version(name) for name in ("deepagents", "langchain", "langchain-core", "langgraph", "langchain-openai")}


def preflight():
    from tests.unit_tests.workflows.test_five_analysis import RoutingFakeModel
    suites = ["tests.unit_tests.workflows.test_five_analysis", "tests.unit_tests.agents.test_main_agent",
              "tests.unit_tests.agents.test_analysis_evidence_review", "tests.unit_tests.workflows.test_analysis_focus_recovery"]
    with selected("create_agent"):
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(suites))
    if not result.wasSuccessful():
        raise RuntimeError("Candidate failed existing offline behavioral tests")

    @tool
    def submit_exploration(report: str) -> str:
        """提交本任务的最终报告."""
        return report

    records = []
    for condition in ("deep_agent", "create_agent", "create_agent", "deep_agent"):
        timings = []
        graph = None
        for _ in range(5):
            loop = FiveAnalysisLoop(AnalysisExecution(), 2, lambda: HumanMessage(content="fixed"), lambda: False, [submit_exploration])
            started = time.perf_counter()
            graph = (adapter if condition == "create_agent" else BASE)(RoutingFakeModel(), "fixed", loop)
            timings.append(time.perf_counter() - started)
        records.append({"condition": condition, "assembly_ms_median": median(timings) * 1000,
                        "graph_nodes": sorted(graph.get_graph().nodes), "assembly_samples": 5})
    save(ROOT / "preflight.json", {"versions": source_versions(), "offline_tests": result.testsRun,
                                   "passed": True, "assembly": records})


def _timeout(signum, frame):
    raise TimeoutError("evaluation wall-clock limit: 240 seconds")


def trial(condition, number):
    sample = json.loads((ROOT / "fixtures/samples.json").read_text())[1]
    image = Path(sample["image"]).read_bytes()
    assert hashlib.sha256(image).hexdigest() == sample["sha256"]
    directory = ROOT / f"trial-{number}-{condition}"
    directory.mkdir(exist_ok=False)
    (directory / "reference.png").write_bytes(image)
    reference = "data:image/png;base64," + base64.b64encode(image).decode()
    model = build_model()
    requests = []
    original = AnalysisChatOpenAI._get_request_payload

    def capture(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        requests.append(request_summary(payload))
        return payload

    started = time.monotonic()
    execution = AnalysisExecution()
    error = None
    with TaskStore(directory) as store:
        store.register("worker", sample["sha256"], {"target": sample["target"], "direction": sample["direction"],
                                                   "user_request": sample["user_request"]})
        attempt = store.start_attempt("worker")
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(240)
        try:
            with selected(condition), patch.object(AnalysisChatOpenAI, "_get_request_payload", capture):
                run_exploration(store, attempt, model, OPTIONS, execution, sample["user_request"], reference,
                                Element.model_validate(sample["target"]), sample["direction"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if store.is_active(attempt):
                store.fail_attempt(attempt, error, permanent=True)
        finally:
            signal.alarm(0)
        status = store.task_view("worker").status
        report = store.read_result("worker") if status == "succeeded" else None
        if report is not None:
            ExplorationSubmission.model_validate(report)
            save(directory / "report.json", report)
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        usage = [event for event in events if event["event"] == "model_usage"]
        tools = [json.loads(line) for line in (directory / "tools.jsonl").read_text().splitlines()] if (directory / "tools.jsonl").exists() else []
        record = {"condition": condition, "number": number, "model": model.model_name, "sample_sha256": sample["sha256"],
                  "status": status, "error": error, "elapsed_seconds": time.monotonic() - started,
                  "calls": execution.model_calls, "network_attempts": len(execution.request_attempts),
                  "repairs": store.task_view("worker").repairs, "requests": requests, "usage": usage,
                  "tool_outcomes": [{"tool": t.get("tool"), "outcome": t.get("outcome")} for t in tools],
                  "counts": {key: len(report.get(key, [])) for key in ["features", "relations", "mechanisms", "sketches"]} if report else None}
    save(directory / "result.json", record)
    print(json.dumps({k: record[k] for k in ["condition", "number", "status", "calls", "elapsed_seconds", "error"]}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preflight", "trial"])
    parser.add_argument("--condition", choices=["deep_agent", "create_agent"])
    parser.add_argument("--number", type=int)
    args = parser.parse_args()
    if args.mode == "preflight":
        preflight()
    else:
        trial(args.condition, args.number)


if __name__ == "__main__":
    main()
