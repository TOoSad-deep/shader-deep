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
from shader_deep.domain.scene_observations import REFERENCE_MEASUREMENT_LIMIT, ObservationIssue, SceneObservations
from shader_deep.domain.scene_planning import ObservationRevisionRequest
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.infrastructure.storage.observation_package import read_observation_package, write_observation_package
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.workflows.analysis.contracts import AnalysisRevision
from shader_deep.workflows.analysis.entry import run_analysis
from shader_deep.workflows.options import AnalysisOptions
from shader_deep.workflows.planning import PlanningOptions, run_planning
from shader_deep.workflows.planning.cascade import observation_impact
from shader_deep.workflows.planning.configuration import apply_planning_overrides, load_planning_options
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call as observation_call, context
from tests.unit_tests.agents.test_native_planning import PlanningModel, call, fixed_context, observation_package, planning
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response

if TYPE_CHECKING:
    import httpx2
    from langchain_core.messages import AIMessage

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

    def test_planning_revision_uses_fixed_prior_observations_and_publishes_new_package(self) -> None:
        source = observation_package(self.directory / "old-observation")
        old_manifest = (source / "manifest.json").read_bytes()
        revision = {"disputed_ids": ["R1"], "contradiction": "覆盖的方向可能反了", "suggested_region": {"left": 0, "top": 1, "right": 3, "bottom": 4}}
        model = PlanningModel(responses=[call("stop_planning", {"reason": "需核对覆盖方向", "observation_revision_request": revision})])
        stopped = run_planning(source, "规划", options=PlanningOptions(output_dir=self.directory / "runs"), model=model)
        self.assertEqual(stopped.status, "failed")
        self.assertEqual(stopped.execution["observation_revision_request"], revision)
        self.assertIsNone(stopped.report_dir)
        observer = NativeRoutingModel()
        outcome = run_analysis(
            source / "reference.png",
            "核实环节",
            options=AnalysisOptions(output_dir=self.directory / "runs"),
            revision=AnalysisRevision(observation_dir=source, request=stopped.observation_revision_request),
            model=observer,
        )
        self.assertEqual(outcome.stop_reason, "completed")
        previous = context(observer.requests[0])["observation_revision"]
        self.assertEqual(previous["previous_observations"]["relations"][0]["id"], "R1")
        self.assertEqual(previous["request"], revision)
        self.assertEqual((source / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(read_observation_package(outcome.report_dir)[1].relations[0].id, "R2")

    def test_risk_probe_is_round_tripped_by_normal_planning_entry(self) -> None:
        body = planning()
        body["sketches"][0]["risk_probes"] = [{"target_ids": ["R1"], "reason": "需要对照交叠", "context_mode": "proxy_context"}]
        model = PlanningModel(responses=[call("save_planning_draft", {"report": body})])
        result = self.run_case(model=model)
        self.assertEqual(result.status, "completed")
        probe = read_planning_package(result.report_dir).report.sketches[0].risk_probes[0]
        self.assertEqual(probe.target_ids, ("R1",))
        self.assertEqual(probe.context_mode, "proxy_context")

    def test_revision_inherits_full_anchor_budget_and_diffs_only_new_evidence(self) -> None:
        reference = observation_package(self.directory / "fixture") / "reference.png"
        options = AnalysisOptions(output_dir=self.directory / "runs")

        def measure(left: int, ordinal: int, question: str) -> AIMessage:
            region = {"left": left, "top": 0, "right": left + 1, "bottom": 1}
            return observation_call("region_stats", {"region": region, "question": question}, ordinal)

        old_model = NativeRoutingModel(responses=[measure(i, i + 1, "首轮验证色场") for i in range(REFERENCE_MEASUREMENT_LIMIT)])
        before = run_analysis(reference, "分析全图", options=options, model=old_model)
        old = read_observation_package(before.report_dir)
        anchors = old[0].reference_measurements
        self.assertEqual(len(anchors), REFERENCE_MEASUREMENT_LIMIT)
        request = ObservationRevisionRequest(
            disputed_ids=("F8",), contradiction="核实局部色场", suggested_region=PixelRegion(left=6, top=0, right=8, bottom=2)
        )
        model = NativeRoutingModel(responses=[measure(0, 1, "旧测量重查"), measure(6, 2, "新区域补测")])
        revision = AnalysisRevision(observation_dir=before.report_dir, request=request)
        after = run_analysis(reference, "分析全图", options=options, revision=revision, model=model)
        current = read_observation_package(after.report_dir)
        self.assertEqual(current[0].reference_measurements[:-1], anchors)
        self.assertEqual(current[0].reference_measurements[-1].region.left, 6)
        self.assertEqual(read_observation_package(before.report_dir), old)
        impact = observation_impact(old, current, request)
        self.assertEqual(impact.changed_metadata, ("reference_measurements",))
        self.assertEqual(set(impact.planning), {"E1", "F8", "R2"})
        self.assertFalse(impact.criteria)

    def test_empty_scene_can_request_revision_for_an_omitted_object(self) -> None:
        source = observation_package(self.directory / "fixture")
        manifest, observed = read_observation_package(source)
        empty = SceneObservations(elements=(), features=(), relations=())
        write_observation_package(self.directory / "empty", empty, manifest, (source / "reference.png").read_bytes())
        request = {"disputed_ids": [], "contradiction": "疑似漏掉圆形前景", "suggested_region": {"left": 0, "top": 0, "right": 4, "bottom": 4}}
        planner = PlanningModel(responses=[call("stop_planning", {"reason": "需核对漏检", "observation_revision_request": request})])
        stopped = run_planning(self.directory / "empty", "规划", options=PlanningOptions(output_dir=self.directory / "runs"), model=planner)
        self.assertEqual(stopped.execution["observation_revision_request"], request)
        with self.assertRaisesRegex(ValueError, "关联的现有"):
            stopped.observation_revision_request.validate_against(observed, (8, 5))
        outcome = run_analysis(
            source / "reference.png",
            "分析全图",
            options=AnalysisOptions(output_dir=self.directory / "runs"),
            revision=AnalysisRevision(observation_dir=self.directory / "empty", request=stopped.observation_revision_request),
            model=NativeRoutingModel(),
        )
        self.assertIn("F8", read_observation_package(outcome.report_dir)[1].identities)

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
