"""通过真实工具验证暂时性错误恢复预算及简短的参数校验反馈."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx2
from langchain.tools import tool

from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.tool_json import recover_object
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from tests.unit_tests._analysis_fixture import AnalysisFixture
from tests.unit_tests._generation_fixture import tool_results
from tests.unit_tests.test_analysis import context_payload, draft_batch, report_arguments, synthesis_arguments

if TYPE_CHECKING:
    from collections.abc import Iterator


class InterruptedStream(httpx2.SyncByteStream):
    def __init__(self, first_frame: bytes) -> None:
        self.first_frame = first_frame

    def __iter__(self) -> Iterator[bytes]:
        yield self.first_frame
        msg = "Response stream interrupted"
        raise httpx2.ReadError(msg)


class AnalysisRecoveryTests(AnalysisFixture):
    def setUp(self) -> None:
        super().setUp()
        # 恢复用例显式选择有限模式, 不依赖正式运行的不限调用默认值.
        self.options = AnalysisOptions(output_dir=self.root / "analysis", max_tasks=2, max_main_calls=8, max_worker_calls=3, max_request_retries=1)
        self.response = self.analyze

    def respond(self, client: httpx2.Client, request: httpx2.Request, **kwargs: object) -> httpx2.Response:
        pending: set[str] = set()
        for message in json.loads(request.content)["messages"]:
            if message["role"] == "tool" and message.get("tool_call_id") in pending:
                pending.remove(message["tool_call_id"])
                continue
            if pending or message["role"] == "tool":
                return httpx2.Response(400, request=request, json={"error": {"message": "Unpaired tool history", "type": "invalid_request_error"}})
            if message["role"] == "assistant":
                calls = message.get("tool_calls", [])
                if not message.get("content") and not calls:
                    return httpx2.Response(
                        400, request=request, json={"error": {"message": "Empty assistant message", "type": "invalid_request_error"}}
                    )
                pending.update(call["id"] for call in calls)
        if pending:
            return httpx2.Response(400, request=request, json={"error": {"message": "Missing tool responses", "type": "invalid_request_error"}})
        return super().respond(client, request, **kwargs)

    def analyze(self, request: dict[str, object]) -> dict[str, object]:
        payload = context_payload(request)
        if payload["task"]["lens_config"] is not None:
            return self.call("submit_analysis_report", report_arguments(payload))
        if payload["related_results"]:
            return self.call("finish_analysis", synthesis_arguments(payload))
        return self.call("run_analysis_batch", {"requests": draft_batch()})

    def run_case(self) -> tuple[AnalysisOutcome, dict[str, object]]:
        with patch("shader_deep.analysis.transport.time.sleep"):
            outcome = run_analysis(self.root / "reference.PNG", "Analyze", options=self.options)
        return outcome, json.loads((outcome.run_dir / "run.json").read_text())

    def test_transient_failure_recovers_without_new_tasks_or_replaying_siblings(self) -> None:
        failed = set()

        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            identifier = payload["task"]["id"]
            if identifier not in failed:
                failed.add(identifier)
                msg = "Temporary transport failure"
                raise httpx2.ConnectError(msg)
            return self.analyze(request)

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", saved)
        self.assertEqual(len(saved["worker_executions"]), 2)
        self.assertEqual(saved["main_execution"]["model_calls"], 3)
        self.assertEqual(len(saved["main_execution"]["request_attempts"]), 4)
        for execution in saved["worker_executions"].values():
            self.assertEqual(execution["model_calls"], 1)
            self.assertEqual(len(execution["request_attempts"]), 2)
            self.assertEqual([item["attempt"] for item in execution["request_attempts"]], [1, 2])
            self.assertIsNotNone(execution["request_attempts"][0]["error_type"])
            self.assertIsNone(execution["request_attempts"][1]["error_type"])
        self.assertEqual(len(self.requests), 8)  # 不允许 SDK 内部隐式重试 HTTP 请求.

    def test_exhausted_connection_attempts_preserve_successful_sibling(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            lens = context_payload(request)["task"]["lens_config"]
            if lens is not None and lens["origin"] == "generated":
                msg = "Still disconnected"
                raise httpx2.ConnectError(msg)
            return self.analyze(request)

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "partial")
        failed = [value for value in saved["worker_executions"].values() if value["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(failed[0]["request_attempts"]), 2)
        self.assertEqual(len(outcome.summary_result.analysis_detail.source_result_ids), 1)
        self.assertEqual(len(self.requests), 6)

    def test_local_nontransient_errors_are_not_retried(self) -> None:
        def response(_request: dict[str, object]) -> dict[str, object]:
            msg = "Local programming defect"
            raise RuntimeError(msg)

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "error")
        self.assertEqual(len(saved["main_execution"]["request_attempts"]), 1)
        self.assertEqual(len(self.requests), 1)

    def test_wrong_summary_nesting_has_short_actionable_feedback(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            if payload["task"]["lens_config"] is not None and not tool_results(request):
                arguments = report_arguments(payload)
                arguments["report"]["summary"] = "LONG_INVALID_BODY" * 1000
                return self.call("submit_analysis_report", arguments)
            return self.analyze(request)

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertTrue(all(execution["tool_feedback"] for execution in saved["worker_executions"].values()))
        messages = [str(result) for request in self.requests for result in tool_results(request) if "report.summary" in str(result)]
        self.assertTrue(messages)
        for message in messages:
            self.assertLess(len(message), 1600)
            self.assertNotIn("LONG_INVALID_BODY", message)

    def test_incomplete_streams_retry_before_any_tool_execution(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.requests.clear()

                def send(client: httpx2.Client, request: httpx2.Request, *, break_stream: bool = interrupted, **kwargs: object) -> httpx2.Response:
                    response = self.respond(client, request, **kwargs)
                    if len(self.requests) != 1:
                        return response
                    first_frame = response.content.split(b"\n\n", 1)[0] + b"\n\n"
                    if break_stream:
                        return httpx2.Response(
                            200, request=request, stream=InterruptedStream(first_frame), headers={"content-type": "text/event-stream"}
                        )
                    return httpx2.Response(200, request=request, content=first_frame, headers={"content-type": "text/event-stream"})

                with patch("httpx2.Client.send", new=send):
                    outcome, saved = self.run_case()
                self.assertEqual(outcome.stop_reason, "completed")
                self.assertEqual(len(saved["worker_executions"]), 2)
                self.assertEqual(len(saved["main_execution"]["request_attempts"]), 4)
                self.assertEqual(len(self.requests), 6)

    def test_deepseek_budget_uses_honored_wire_parameter(self) -> None:
        with patch.dict(
            os.environ, {"DS_MICU_MODEL": "deepseek-fixture", "DS_MICU_BASE_URL": "https://shader-deep.invalid/v1", "DS_MICU_API_KEY": "fixture-key"}
        ):
            outcome, _ = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        for request in self.requests:
            self.assertEqual(request["max_tokens"], self.options.max_output_tokens)
            self.assertNotIn("max_completion_tokens", request)

    def test_output_limit_repairs_in_next_logical_call_without_executing_partial_tool(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            message = self.analyze(request)
            if len(self.requests) == 1:
                message["_fixture_finish_reason"] = "length"
            return message

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(len(saved["worker_executions"]), 2)
        self.assertEqual(saved["main_execution"]["model_calls"], 4)
        self.assertEqual(len(saved["main_execution"]["request_attempts"]), 4)
        self.assertEqual(saved["main_execution"]["tool_feedback"][0]["tool"], "output_budget")
        self.assertTrue(any("更短的结构化结果" in str(request["messages"]) for request in self.requests))

    def test_schema_valid_object_with_surplus_closer_needs_no_extra_model_call(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            message = self.analyze(request)
            for call in message.get("tool_calls", []):
                call["function"]["arguments"] += "}"
            return message

        self.response = response
        outcome, saved = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", saved)
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(saved["main_execution"]["model_calls"], 3)
        self.assertEqual(len(outcome.summary_result.analysis_detail.source_result_ids), 2)
        self.assertTrue(any("surplus closing" in item["message"] for item in saved["main_execution"]["tool_feedback"]))

    def test_raw_arguments_cannot_be_accepted_by_partial_or_permissive_parsing(self) -> None:
        for streaming in (False, True):
            for corruption in ("missing_closer", "duplicate_key"):
                with self.subTest(streaming=streaming, corruption=corruption):
                    self.requests.clear()
                    self.options = replace(self.options, stream_model_responses=streaming)

                    def response(request: dict[str, object], *, mode: str = corruption) -> dict[str, object]:
                        message = self.analyze(request)
                        if len(self.requests) == 1:
                            call = message["tool_calls"][0]["function"]
                            raw = call["arguments"]
                            call["arguments"] = raw[:-1] if mode == "missing_closer" else '{"requests": [],' + raw[1:]
                        return message

                    self.response = response
                    outcome, saved = self.run_case()
                    self.assertEqual(outcome.stop_reason, "completed", saved)
                    self.assertEqual(saved["main_execution"]["model_calls"], 4)
                    self.assertEqual(len(saved["worker_executions"]), 2)
                    self.assertEqual(saved["main_execution"]["tool_feedback"][0]["category"], "json_syntax")
                    self.assertEqual(saved["main_execution"]["format_repair_calls"], 1)
                    repair_request = self.requests[1]
                    self.assertEqual(context_payload(repair_request)["related_results"], [])
                    self.assertEqual(tool_results(repair_request), [])
                    rejected = [message for message in repair_request["messages"] if message["role"] == "assistant"]
                    self.assertTrue(rejected)
                    self.assertTrue(all(message.get("content") and not message.get("tool_calls") for message in rejected))

    def test_repeated_truncation_stops_after_two_repairs_without_executing_tools(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            message = self.analyze(request)
            message["_fixture_finish_reason"] = "length"
            return message

        self.response = response
        outcome, saved = self.run_case()
        self.assertIsNone(outcome.summary_result)
        self.assertEqual(saved["main_execution"]["status"], "stopped")
        self.assertIn("format-repair budget", saved["main_execution"]["error"])
        self.assertEqual(saved["main_execution"]["model_calls"], 3)
        self.assertEqual(saved["main_execution"]["format_repair_calls"], 2)
        self.assertEqual(saved["worker_executions"], {})

    def test_json_recovery_does_not_guess_missing_fields_or_discard_extra_objects(self) -> None:
        @tool
        def submit_number(value: int) -> str:
            """提交一个数字, 供解析器对照测试使用."""
            return str(value)

        for value in ('{"value":1}{"value":2}', '{"value":1} trailing', '{"missing":1}}', '{"value":1'):
            self.assertIsNone(recover_object(value, submit_number))
        self.assertEqual(recover_object('{"value":1}}', submit_number), {"value": 1})

    def test_unrecoverable_json_supplies_precise_location_in_next_image_context(self) -> None:
        attempted = set()

        def response(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            task = payload["task"]
            message = self.analyze(request)
            if task["lens_config"] is not None and task["id"] not in attempted:
                attempted.add(task["id"])
                message["tool_calls"][0]["function"]["arguments"] = '{"summary": "bad "quote", "report": {}}'
            return message

        self.response = response
        outcome, _ = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertTrue(any("JSON syntax error:" in str(request["messages"]) and "column" in str(request["messages"]) for request in self.requests))
