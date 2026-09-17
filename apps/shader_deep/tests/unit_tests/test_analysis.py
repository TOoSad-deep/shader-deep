"""使用无网络模型传输替身, 验证真实 Deep Agents 流程与工具参数结构."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from shader_deep import analysis_cli
from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.presets import PRESET_LENSES
from shader_deep.analysis.schemas import AnalysisTaskRequest, LensReport
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from shader_deep.blackboard import add_result, add_task
from shader_deep.schemas import ResultRecord, TaskRecord
from tests.unit_tests._analysis_fixture import AnalysisFixture, context_payload
from tests.unit_tests._generation_fixture import tool_results
from tests.unit_tests.test_context import PNG


def draft_batch() -> list[dict[str, object]]:
    return [
        {"preset_id": "graphics-2d", "objective": "Inspect visible curves"},
        {
            "lens": {
                "name": "Periodicity",
                "focus": "Inspect repetition",
                "method_notes": ["Check spacing"],
                "default_questions": ["Is spacing uniform?"],
            },
            "objective": "Explain repetition",
        },
    ]


def report_arguments(payload: dict[str, object]) -> dict[str, object]:
    name = payload["task"]["lens_config"]["name"]
    return {
        "summary": f"Independent report from {name}",
        "report": {
            "scope": "Whole image",
            "observations": [{"id": "O1", "text": f"Observation from {name}", "region": "center"}],
            "interpretations": [
                {
                    "id": "H1",
                    "text": "A possible carrier",
                    "supporting_observation_ids": ["O1"],
                    "uncertainties": ["Other explanations remain possible"],
                }
            ],
            "uncertainties": ["Single-image ambiguity"],
            "suggestions": ["Compare candidate mechanisms later"],
        },
    }


def synthesis_arguments(payload: dict[str, object]) -> dict[str, object]:
    reports = [result for result in payload["related_results"] if result["analysis_detail"] is not None]
    return {
        "text": "Analysis complete with competing explanations",
        "summary": {
            "source_result_ids": [result["id"] for result in reports],
            "key_observations": [{"text": "Visible structure", "source_refs": [{"result_id": result["id"], "item_id": "O1"} for result in reports]}],
            "hypotheses": [{"text": "Possible mechanism", "source_refs": [{"result_id": reports[0]["id"], "item_id": "H1"}]}],
            "open_questions": [{"text": "Depth is uncertain", "source_refs": [{"result_id": reports[0]["id"]}]}],
        },
    }


class AnalysisTests(AnalysisFixture):
    def setUp(self) -> None:
        super().setUp()
        self.analysis_options = AnalysisOptions(output_dir=self.root / "analysis", max_tasks=3, max_parallel=2, max_worker_calls=2, max_main_calls=5)
        self.response = self.analyze

    def analyze(self, request: dict[str, object]) -> dict[str, object]:
        payload = context_payload(request)
        if payload["task"]["lens_config"] is not None:
            return self.call("submit_analysis_report", report_arguments(payload))
        if payload["related_results"]:
            return self.call("finish_analysis", synthesis_arguments(payload))
        message = self.call("run_analysis_batch", {"requests": draft_batch()})
        message["content"] = "COORDINATOR_PRIVATE_HISTORY"
        return message

    def run_case(self, **changes: object) -> AnalysisOutcome:
        return run_analysis(self.root / "reference.PNG", "Analyze the structure", options=replace(self.analysis_options, **changes))

    def manifest(self, outcome: AnalysisOutcome) -> dict[str, object]:
        return json.loads((outcome.run_dir / "run.json").read_text())

    def test_dynamic_lens_parallel_isolated_images_and_saved_provenance(self) -> None:
        barrier = threading.Barrier(2, timeout=10)

        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is not None:
                barrier.wait()  # 通过并发屏障检查实际重叠执行, 串行执行无法通过该断言.
                self.assertEqual(payload["related_results"], [])
                self.assertEqual(payload["child_tasks"], [])
                self.assertEqual(payload["preset_lenses"], [])
                self.assertNotIn("COORDINATOR_PRIVATE_HISTORY", str(request["messages"]))
                self.assertIn("data:image/png;base64,", str(request["messages"]))
                names = {item["function"]["name"] for item in request["tools"]}
                self.assertEqual(names, {"submit_analysis_report", "repair_analysis_submission"})
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        children = [task for task in outcome.state["tasks"].values() if task.parent_task_id == "A1"]
        self.assertEqual({task.lens_config.origin for task in children}, {"preset", "generated"})
        self.assertEqual(len(children), 2)
        self.assertEqual((outcome.run_dir / "reference.png").read_bytes(), PNG)
        self.assertEqual(list(outcome.run_dir.glob("*.glsl")), [])
        manifest = self.manifest(outcome)
        self.assertEqual(manifest["main_execution"]["model_calls"], 3)  # 派发、实际读取、综合.
        self.assertTrue(all(value["model_calls"] == 1 for value in manifest["worker_executions"].values()))
        self.assertNotIn("fixture-key", json.dumps(manifest))
        detail = outcome.summary_result.analysis_detail
        self.assertEqual(len(detail.source_result_ids), 2)
        self.assertEqual(detail.missing_task_ids, ())
        main_context = context_payload(self.requests[-1])
        self.assertEqual({task["lens_config"]["origin"] for task in main_context["child_tasks"]}, {"preset", "generated"})

    def test_invalid_report_reference_is_repaired_through_real_tool_feedback(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is not None and not tool_results(request):
                arguments = report_arguments(payload)
                arguments["report"]["interpretations"][0]["supporting_observation_ids"] = ["absent"]
                return self.call("submit_analysis_report", arguments)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual([value["model_calls"] for value in self.manifest(outcome)["worker_executions"].values()], [2, 2])
        self.assertTrue(any("Invalid observation references" in str(tool_results(request)) for request in self.requests))

    def test_image_delivery_survives_endpoint_that_rejects_adjacent_user_turns(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            messages = request["messages"]
            adjacent_users = any(left["role"] == right["role"] == "user" for left, right in pairwise(messages))
            current = messages[-1]
            visible = (
                current["role"] == "user"
                and isinstance(current["content"], list)
                and any(block.get("type") == "image_url" for block in current["content"])
            )
            if adjacent_users or not visible:
                return {"role": "assistant", "content": "No visible image in the current turn"}
            payload = context_payload(request)
            if payload["task"]["lens_config"] is not None and not tool_results(request):
                arguments = report_arguments(payload)
                arguments["report"]["interpretations"][0]["supporting_observation_ids"] = ["absent"]
                return self.call("submit_analysis_report", arguments)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(len(outcome.summary_result.analysis_detail.source_result_ids), 2)
        self.assertTrue(any("Invalid observation references" in str(tool_results(request)) for request in self.requests))
        # 每轮刷新请求上下文时, 历史中不应不断累积旧的图片附件.
        for request in self.requests:
            images = [
                block
                for message in request["messages"]
                if isinstance(message["content"], list)
                for block in message["content"]
                if block.get("type") == "image_url"
            ]
            self.assertEqual(len(images), 1)

    def test_summary_cannot_promote_interpretation_to_observation(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"] and not attempted:
                attempted = True
                arguments = synthesis_arguments(payload)
                arguments["summary"]["key_observations"][0]["source_refs"][0]["item_id"] = "H1"
                return self.call("finish_analysis", arguments)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(self.manifest(outcome)["main_execution"]["model_calls"], 4)
        self.assertTrue(any("Invalid observation reference" in str(tool_results(request)) for request in self.requests))

    def test_worker_budget_failure_preserves_other_report_and_marks_partial(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            lens = payload["task"]["lens_config"]
            if lens is not None and lens["origin"] == "generated":
                return {"role": "assistant", "content": "Still thinking"}
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "partial", self.manifest(outcome))
        detail = outcome.summary_result.analysis_detail
        self.assertEqual(len(detail.source_result_ids), 1)
        self.assertEqual(len(detail.missing_task_ids), 1)
        execution = self.manifest(outcome)["worker_executions"][detail.missing_task_ids[0]]
        self.assertEqual((execution["status"], execution["model_calls"]), ("stopped", 2))

    def test_supplement_receives_only_explicitly_selected_prior_report(self) -> None:
        selected = []

        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            task = payload["task"]
            if task["lens_config"] is None and len(payload["related_results"]) == len(draft_batch()):
                selected.append(payload["related_results"][0]["id"])
                return self.call(
                    "run_analysis_batch",
                    {
                        "requests": [
                            {
                                "preset_id": "spatial-3d",
                                "objective": "Check a specific ambiguity",
                                "related_result_ids": [selected[0]],
                                "purpose": "verify",
                                "gap": "Depth affects the chosen representation",
                                "expected_evidence": "Occlusion boundaries",
                            }
                        ]
                    },
                )
            if task["lens_config"] is not None and task["related_result_ids"]:
                self.assertEqual([result["id"] for result in payload["related_results"]], selected)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(len(outcome.summary_result.analysis_detail.source_result_ids), 3)
        self.assertEqual(len(selected), 1)

    def test_batch_budget_rejection_does_not_register_extra_tasks(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"] and not attempted:
                attempted = True
                return self.call("run_analysis_batch", {"requests": draft_batch()})
            return self.analyze(request)

        self.response = response
        outcome = self.run_case(max_tasks=2)
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(len(outcome.state["tasks"]), 3)
        self.assertTrue(any("budget exceeded" in str(tool_results(request)) for request in self.requests))

    def test_hidden_worker_delegation_and_write_tools_are_rejected(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            lens = payload["task"]["lens_config"]
            if lens is not None and not tool_results(request):
                if lens["origin"] == "preset":
                    return self.call("write_file", {"file_path": "/escape.txt", "content": "unwanted"})
                return self.call("task", {"subagent_type": "general-purpose", "description": "Start another worker"})
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(len(self.requests), 7)
        self.assertTrue(any("未开放此工具" in str(tool_results(request)) for request in self.requests))

    def test_main_plain_text_stops_without_fabricating_a_summary(self) -> None:
        self.response = lambda _request: {"role": "assistant", "content": "Analysis is complete"}
        outcome = self.run_case(max_main_calls=2)
        self.assertEqual(outcome.stop_reason, "model_limit")
        self.assertIsNone(outcome.summary_result)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.manifest(outcome)["worker_executions"], {})

    def test_optional_no_progress_stop_and_live_request_events(self) -> None:
        def response(_request: dict[str, object]) -> dict[str, object]:
            event_path = next(self.analysis_options.output_dir.glob("run-*/events.jsonl"))
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "request_started")
            self.assertGreater(events[-1]["model_call"], 0)
            return self.call("run_analysis_batch", {"requests": [{"preset_id": "absent", "objective": "bad"}, draft_batch()[1]]})

        self.response = response
        outcome = self.run_case(max_main_calls=0, max_repeated_no_progress=2)
        self.assertEqual(outcome.stop_reason, "no_progress", self.manifest(outcome))
        self.assertIsNone(outcome.summary_result)
        self.assertEqual(self.manifest(outcome)["main_execution"]["model_calls"], 2)
        self.assertEqual(self.manifest(outcome)["worker_executions"], {})

    def test_unlimited_main_and_workers_can_repair_past_call_format_and_graph_limits(self) -> None:
        main_calls = 0
        worker_calls: dict[str, int] = {}
        last_rejected_call = 12
        last_rejected_worker_call = 4

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal main_calls
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None:
                self.assertIsNone(payload["limits"]["model_calls_remaining"])
                main_calls += 1
                if payload["related_results"] and main_calls <= last_rejected_call:
                    return self.call("finish_analysis", {"summary": {}, "text": "Correct the rejected summary"})
            else:
                self.assertIsNone(payload["limits"]["model_calls_remaining"])
                identifier = payload["task"]["id"]
                worker_calls[identifier] = worker_calls.get(identifier, 0) + 1
                if worker_calls[identifier] <= last_rejected_worker_call:
                    return self.call("submit_analysis_report", {"report": {}, "summary": "Correct the rejected report"})
            return self.analyze(request)

        self.response = response
        outcome = self.run_case(max_main_calls=0, max_worker_calls=0)
        saved = self.manifest(outcome)
        self.assertEqual(outcome.stop_reason, "completed", saved)
        self.assertEqual(saved["main_execution"]["model_calls"], 14)
        self.assertEqual(saved["main_execution"]["format_repair_calls"], 11)
        self.assertEqual(len(saved["worker_executions"]), 2)
        for worker in saved["worker_executions"].values():
            self.assertEqual(worker["model_calls"], last_rejected_worker_call + 1)
            self.assertEqual(worker["format_repair_calls"], last_rejected_worker_call)
        self.assertEqual(saved["options"]["max_main_calls"], 0)
        self.assertEqual(saved["options"]["max_worker_calls"], 0)

    def test_malformed_inputs_are_rejected_before_running_models(self) -> None:
        with self.assertRaises(ValidationError):
            TypeAdapter(AnalysisTaskRequest).validate_python(
                {"objective": "inspect", "preset_id": "graphics-2d", "lens": {"name": "n", "focus": "f"}}
            )
        with self.assertRaises(ValidationError):
            TypeAdapter(LensReport).validate_python({"scope": "all", "observations": [], "unknown": True})
        with self.assertRaisesRegex(ValueError, "at least two"):
            AnalysisOptions(max_tasks=1)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            AnalysisOptions(max_parallel=True)
        with self.assertRaises(ValueError):
            run_analysis(self.root / "absent.png", "inspect", options=self.analysis_options)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.analysis_options.output_dir.exists())

    def test_same_batch_finish_cannot_skip_receiving_reports(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and not payload["related_results"]:
                directory = next(self.analysis_options.output_dir.iterdir())
                ids = [f"{directory.name}-a001-report", f"{directory.name}-a002-report"]
                message = self.call("run_analysis_batch", {"requests": draft_batch()})
                finish = self.call(
                    "finish_analysis",
                    {
                        "text": "Blind summary",
                        "summary": {
                            "source_result_ids": ids,
                            "key_observations": [{"text": "Guessed", "source_refs": [{"result_id": ids[0], "item_id": "O1"}]}],
                        },
                    },
                )
                finish["tool_calls"][0]["id"] = "call-blind-summary"
                message["tool_calls"].extend(finish["tool_calls"])
                return message
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertTrue(any("source_not_presented" in str(tool_results(request)) for request in self.requests))
        self.assertNotEqual(outcome.summary_result.summary, "Blind summary")

    def test_invalid_batch_is_atomic_and_can_be_repaired(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and not attempted:
                attempted = True
                return self.call("run_analysis_batch", {"requests": [draft_batch()[0], {"preset_id": "absent", "objective": "bad"}]})
            return self.analyze(request)

        self.response = response
        outcome = self.run_case(max_tasks=2)
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual(len(outcome.state["tasks"]), 3)
        self.assertTrue(any("Unknown preset" in str(tool_results(request)) for request in self.requests))

    def test_unknown_summary_source_is_rejected_before_commit(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"] and not attempted:
                attempted = True
                arguments = synthesis_arguments(payload)
                arguments["summary"]["hypotheses"][0]["source_refs"][0]["result_id"] = "unrelated-report"
                return self.call("finish_analysis", arguments)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertTrue(any("Undeclared source result" in str(tool_results(request)) for request in self.requests))
        manifest = self.manifest(outcome)
        self.assertNotIn("unrelated-report", json.dumps(manifest["blackboard"]))
        self.assertTrue(
            any(
                item["category"] == "business_rejection" and "unrelated-report" in item["message"]
                for item in manifest["main_execution"]["tool_feedback"]
            )
        )

    def test_coordinator_failure_preserves_completed_reports(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"]:
                msg = "Fixture provider unavailable"
                raise RuntimeError(msg)
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "error")
        self.assertIsNone(outcome.summary_result)
        self.assertEqual(len(outcome.state["results"]), 2)
        execution = self.manifest(outcome)["main_execution"]
        self.assertEqual(execution["status"], "failed")
        self.assertTrue(execution["error"])
        self.assertFalse(any(thread.name.startswith("analysis-lens") for thread in threading.enumerate()))

    def test_follow_up_cannot_import_another_analysis_roots_report(self) -> None:
        state = self.state
        for identifier in ("A1", "A2"):
            state = add_task(state, TaskRecord(id=identifier, role="analysis", target_version="T1", objective="Inspect"))
        child = TaskRecord(id="L1", role="analysis", target_version="T1", objective="Inspect", parent_task_id="A1", lens_config=PRESET_LENSES[0])
        state = add_task(state, child)
        report = TypeAdapter(LensReport).validate_python({"scope": "all", "observations": [{"id": "O1", "text": "Visible", "region": "center"}]})
        state = add_result(state, ResultRecord(id="R1", task_id="L1", status="completed", summary="Done", analysis_detail=report))
        with self.assertRaisesRegex(ValueError, "outside parent"):
            add_task(state, replace(child, id="L2", parent_task_id="A2", related_result_ids=("R1",)))
        with self.assertRaisesRegex(ValueError, "root analysis parent"):
            add_task(state, replace(child, id="L2", parent_task_id="G1"))
        self.assertNotIn("L2", state["tasks"])

    def test_input_image_is_fixed_even_when_source_changes_mid_run(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and not payload["related_results"]:
                (self.root / "reference.PNG").write_bytes(b"source changed")
            else:
                self.assertIn("data:image/png;base64,iVBOR", str(request["messages"]))
            return self.analyze(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", self.manifest(outcome))
        self.assertEqual((outcome.run_dir / "reference.png").read_bytes(), PNG)

    def test_cli_emits_real_summary_as_json(self) -> None:
        config = self.root / "analysis.yaml"
        config.write_text("max_output_tokens: 2048\nmax_worker_calls: 4\nstream_model_responses: false\noutput_dir: analysis\n", encoding="utf-8")
        arguments = ["shader-deep-analyze", str(self.root / "reference.PNG"), "Inspect", "--config", str(config)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = analysis_cli.main()
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        manifest = json.loads((Path(payload["run_dir"]) / "run.json").read_text())
        self.assertEqual(payload["result"], manifest["blackboard"]["results"][manifest["summary_result_id"]])
        self.assertIn("Status: completed", stderr.getvalue())
        self.assertEqual(Path(payload["run_dir"]).parent, self.root / "analysis")
        self.assertEqual(manifest["options"]["max_worker_calls"], 4)
        self.assertEqual(manifest["options"]["max_output_tokens"], 2048)
        self.assertEqual({request["max_completion_tokens"] for request in self.requests}, {2048})
        self.assertTrue(all(not request.get("stream", False) for request in self.requests))
