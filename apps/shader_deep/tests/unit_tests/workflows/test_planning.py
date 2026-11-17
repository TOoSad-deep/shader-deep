"""通过公开规划入口验证发布、配置、失败和 CLI 索引."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.cli.planning import main
from shader_deep.domain.scene_observations import ObservationIssue
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.workflows.planning import PlanningOptions, run_planning
from shader_deep.workflows.planning.configuration import apply_planning_overrides, load_planning_options
from tests.unit_tests.agents.test_native_planning import PlanningModel, call, fixed_context, observation_package, planning
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response

if TYPE_CHECKING:
    import httpx2

    from shader_deep.workflows.planning.contracts import PlanningOutcome


class PlanningWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_case(self, *, gaps: tuple[ObservationIssue, ...] = (), model: PlanningModel | None = None) -> PlanningOutcome:
        source = observation_package(self.directory / "observation", gaps=gaps)
        with patch("shader_deep.workflows.planning.entry.build_model", return_value=model or PlanningModel()):
            return run_planning(source, "规划两个圆的全图方案", options=PlanningOptions(output_dir=self.directory / "runs", max_request_retries=0))

    def test_public_entry_publishes_a_formally_readable_whole_scene(self) -> None:
        outcome = self.run_case()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.stop_reason, "completed")
        package = read_planning_package(outcome.report_dir)
        self.assertEqual(package.source.request, "规划两个圆的全图方案")
        self.assertEqual(package.source.observations.relations[0].id, "R1")
        self.assertEqual(outcome.execution["model_calls"], 3)
        self.assertIsNone(outcome.execution["reasoning_effort"])
        result = json.loads((outcome.run_dir / "result.json").read_text())
        self.assertEqual(result["report_path"], "report")
        self.assertEqual(result["execution"]["accepted_revision"], result["execution"]["seen_revision"])

    def test_partial_planning_keeps_upstream_element_gap(self) -> None:
        gap = ObservationIssue(refs=("E2",), description="圆形边界遮挡较重")
        outcome = self.run_case(gaps=(gap,))
        self.assertEqual(outcome.status, "partial")
        package = read_planning_package(outcome.report_dir)
        self.assertEqual(package.manifest.status, "partial")
        self.assertEqual(package.source.observation_manifest.gaps, (gap,))

    def test_nondefault_generation_conditions_enter_actual_requests(self) -> None:
        source = observation_package(self.directory / "observation")
        model = PlanningModel()
        render = GenerationOptions(width=40, height=30, time=2.25)
        with patch("shader_deep.workflows.planning.entry.build_model", return_value=model):
            outcome = run_planning(source, "规划全图", options=PlanningOptions(output_dir=self.directory / "runs"), generation_options=render)
        constraints = read_planning_package(outcome.report_dir).source.constraints
        self.assertEqual((constraints.width, constraints.height, constraints.time), (40, 30, 2.25))
        self.assertTrue(all(fixed_context(messages)["generation_constraints"]["time"] == render.time for messages in model.requests))

    def test_publish_failure_returns_failed_with_frozen_submission(self) -> None:
        with patch("shader_deep.workflows.planning.entry.write_planning_package", side_effect=OSError("disk full")):
            outcome = self.run_case()
        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.report_dir)
        self.assertIn("publish_error", outcome.error)
        self.assertTrue((outcome.run_dir / "results/planning.json").is_file())
        self.assertEqual(json.loads((outcome.run_dir / "result.json").read_text())["status"], "failed")

    def test_model_exception_and_stop_leave_explicit_failed_indices(self) -> None:
        source = observation_package(self.directory / "observation")
        with patch("shader_deep.workflows.planning.entry._invoke", side_effect=RuntimeError("test transport failure")):
            outcome = run_planning(source, "规划", options=PlanningOptions(output_dir=self.directory / "runs"))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("test transport failure", outcome.error)
        with patch(
            "shader_deep.workflows.planning.entry.build_model",
            return_value=PlanningModel(responses=[call("stop_planning", {"reason": "需要补充观察"})]),
        ):
            stopped = run_planning(source, "规划", options=PlanningOptions(output_dir=self.directory / "runs"))
        self.assertEqual(stopped.status, "failed")
        self.assertEqual(stopped.error, "需要补充观察")

    def test_interrupt_is_preserved_after_result_index(self) -> None:
        source = observation_package(self.directory / "observation")
        interrupted = KeyboardInterrupt()
        with patch("shader_deep.workflows.planning.entry._invoke", side_effect=interrupted), self.assertRaises(KeyboardInterrupt) as caught:
            run_planning(source, "规划", options=PlanningOptions(output_dir=self.directory / "runs"))
        self.assertIs(caught.exception, interrupted)
        result = json.loads(next((self.directory / "runs").glob("run-*/result.json")).read_text())
        self.assertEqual(result["status"], "failed")

    def test_configuration_rejects_unknowns_and_resolves_relative_output(self) -> None:
        path = self.directory / "planning.yaml"
        path.write_text("max_model_calls: 7\noutput_dir: outputs\n", encoding="utf-8")
        options = load_planning_options(path)
        self.assertEqual(options.max_model_calls, 7)
        self.assertEqual(options.output_dir, self.directory.resolve() / "outputs")
        self.assertEqual(apply_planning_overrides(options, {"max_model_calls": 4}).max_model_calls, 4)
        for content in ("unknown: 1\n", "max_model_calls: true\n", "max_model_calls: 0\n", "stream_model_responses: 1\n", "1: value\n"):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises((TypeError, ValueError)):
                load_planning_options(path)

    def test_cli_uses_yaml_then_explicit_override_and_json_stdout(self) -> None:
        source = observation_package(self.directory / "observation")
        config = self.directory / "planning.yaml"
        config.write_text("max_model_calls: 7\noutput_dir: runs\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ["shader-deep-plan", str(source), "规划全图", "--config", str(config), "--max-model-calls", "4"]
        with (
            patch("sys.argv", args),
            patch("shader_deep.workflows.planning.entry.build_model", return_value=PlanningModel()),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main()
        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        result = json.loads((Path(payload["run_dir"]) / "result.json").read_text())
        self.assertEqual(result["options"]["max_model_calls"], 4)
        self.assertEqual(payload["status"], "completed")
        self.assertIn("Status: completed", stderr.getvalue())

    def test_cli_invalid_budget_is_an_input_error(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["shader-deep-plan", "missing", "规划", "--max-model-calls", "0"]), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("max_model_calls", stderr.getvalue())

    def test_cli_default_output_limit_reaches_actual_provider_payload(self) -> None:
        source = observation_package(self.directory / "observation")
        self.assertEqual(PlanningOptions().max_output_tokens, 32768)
        self.assertEqual(load_planning_options().max_output_tokens, 32768)
        for override, expected in ((None, 32768), (2048, 2048)):
            with self.subTest(override=override):
                self._check_provider_output_limit(source, override, expected)

    def _check_provider_output_limit(self, source: Path, override: int | None, expected: int) -> None:
        requests: list[dict] = []
        calls = (
            ("save_planning_draft", {"report": planning()}),
            ("read_file", {"file_path": "/draft/planning.json", "limit": 2000}),
            ("finish_planning", {}),
        )

        def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            requests.append(json.loads(request.content))
            name, arguments = calls[len(requests) - 1]
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call-{len(requests)}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
            }
            return _response(request, message, streaming=True)

        args = ["shader-deep-plan", str(source), "规划全图", "--output-dir", str(self.directory / "runs")]
        if override is not None:
            args.extend(("--max-output-tokens", str(override)))
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, OPENROUTER, clear=True),
            patch("sys.argv", args),
            patch("httpx2.Client.send", autospec=True, side_effect=respond),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0, stderr.getvalue())
        self.assertEqual(len(requests), len(calls))
        for payload in requests:
            self.assertEqual(payload["max_tokens"], expected)
            self.assertNotIn("max_completion_tokens", payload)
            self.assertNotIn("reasoning", payload)
        result = json.loads((Path(json.loads(stdout.getvalue())["run_dir"]) / "result.json").read_text())
        self.assertEqual(result["options"]["max_output_tokens"], expected)
        self.assertEqual(result["options"]["max_model_calls"], 20)
