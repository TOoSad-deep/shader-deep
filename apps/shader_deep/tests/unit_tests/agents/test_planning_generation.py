"""正式整图规划通过公开入口生成与局部备选取材的行为."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from dataclasses import replace
from unittest.mock import patch

from shader_deep.agents.generation.agent import _run_config
from shader_deep.agents.generation.context import _build_generation_context
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.agents.generation.tools.session import RenderSession
from shader_deep.api import run_generation_from_planning
from shader_deep.cli.generation import main
from shader_deep.domain.generation import ScenePlanningBinding
from shader_deep.infrastructure.storage.generation_planning_inputs import capture_planning_generation_inputs
from shader_deep.workflows.generation_from_planning import _prepare_planning_generation
from tests.unit_tests.agents.test_generation import ThreadBoundRenderer
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, task_payloads
from tests.unit_tests.fixtures._planning_fixture import planning_report


class PlanningGenerationTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.report = planning_report(self.root, gaps=True)
        self.execution = replace(self.options, width=40, height=30, time=1.5)
        ThreadBoundRenderer.renders = []
        ThreadBoundRenderer.closed = 0
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)

    def test_whole_scene_and_both_gap_sources_reach_requests_and_selected_result(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                shutil.rmtree(self.report)
            return self.render_then_finish(request)

        self.response = respond
        outcome = run_generation_from_planning(self.report, sketch_id="S1", alternative_index=0, options=self.execution)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertIsNotNone(outcome.selected_candidate)
        for request in self.requests:
            payload = task_payloads(request)[0]
            scheme = payload["planning_scheme"]
            self.assertEqual(len(scheme["observations"]["elements"]), 2)
            self.assertEqual(len(scheme["observations"]["relations"]), 2)
            self.assertEqual(scheme["selected"]["choices"]["R9"], ["M1", "M2"])
            self.assertEqual([item["id"] for item in scheme["selected"]["mechanisms"]], ["M1", "M2"])
            self.assertTrue(scheme["selected"]["observation_gaps"])
            self.assertEqual(scheme["selected"]["gaps"][0]["refs"], ["R9"])
        result = next(value for value in outcome.state["results"].values() if value.id.endswith("-selection"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.limitations), 2)
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertFalse(record["inputs"]["planning"]["complete"])
        self.assertEqual(ThreadBoundRenderer.renders, [(40, 30, 1.5)])

    def test_default_conditions_are_inherited_and_explicit_render_changes_are_visible(self) -> None:
        inherited = capture_planning_generation_inputs(self.report, self.root / "default-input", "S1")
        self.assertEqual((inherited.options.width, inherited.options.height, inherited.options.time), (40, 30, 1.5))
        changed = GenerationOptions(width=20, height=15, time=2.0, max_attempts=7)
        overridden = capture_planning_generation_inputs(self.report, self.root / "override-input", "S1", options=changed)
        payload = json.loads(overridden.scheme_json)
        self.assertEqual(payload["execution_render"], {"width": 20, "height": 15, "time": 2.0})
        self.assertEqual(payload["render_overrides"]["time"], {"planned": 1.5, "actual": 2.0})
        self.assertEqual(overridden.options.max_attempts, 7)

    def test_cli_budget_override_keeps_planned_dimensions_and_time(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = [
            "shader-deep",
            "--planning-report",
            str(self.report),
            "--sketch",
            "S1",
            "--max-attempts",
            "5",
            "--output-dir",
            str(self.options.output_dir),
        ]
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(), 0)
        self.assertIn("mainImage", stdout.getvalue())
        self.assertEqual(ThreadBoundRenderer.renders, [(40, 30, 1.5)])
        record = json.loads((next(self.options.output_dir.iterdir()) / "run.json").read_text())
        self.assertEqual(record["max_attempts"], 5)
        self.assertEqual(record["inputs"]["planning"]["render_overrides"], {})

    def test_invalid_selection_fails_before_any_request_and_task_binding_is_checked(self) -> None:
        with self.assertRaises(ValueError):
            run_generation_from_planning(self.report, sketch_id="S99", options=self.execution)
        self.assertEqual(self.requests, [])
        prepared = _prepare_planning_generation(self.report, "S1", options=self.execution)
        state = prepared.state
        original = state["tasks"]["G1"]
        state["tasks"]["G1"] = replace(original, generation_binding=None)
        with self.assertRaisesRegex(ValueError, "binding"):
            _build_generation_context(state, "G1", inputs=prepared.inputs)
        state["tasks"]["G1"] = original
        with self.assertRaisesRegex(ValueError, "prepared"):
            _build_generation_context(state, "G1")

    def test_trace_metadata_links_the_selected_planning_and_observation_versions(self) -> None:
        prepared = _prepare_planning_generation(self.report, "S1", alternative_index=0, options=self.execution)
        session = RenderSession(prepared.state, "G1", self.execution, self.root, run_dir=prepared.run_dir, inputs=prepared.inputs)
        binding = prepared.inputs.binding
        self.assertIsInstance(binding, ScenePlanningBinding)
        metadata = _run_config(session)["metadata"]
        self.assertEqual(metadata["planning_sha256"], binding.content_sha256)
        self.assertEqual(metadata["observation_sha256"], binding.observation_sha256)
        self.assertEqual((metadata["sketch_id"], metadata["alternative_index"]), ("S1", 0))
