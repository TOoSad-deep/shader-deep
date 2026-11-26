"""报告入口复用生成闭环、固定目录和命令行交付语义."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.api import run_generation_from_report
from shader_deep.cli.generation import main
from shader_deep.domain.blackboard import add_candidate
from shader_deep.domain.shader_parameters import ParameterSpec
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters, read_candidate_parameters
from tests.unit_tests.agents.test_context import BASE_CODE
from tests.unit_tests.agents.test_generation import ThreadBoundRenderer
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, generation_report, task_payloads

if TYPE_CHECKING:
    from shader_deep.agents.generation.tools import RenderSession


class ReportGenerationTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.report = generation_report(self.root)
        ThreadBoundRenderer.renders = []
        ThreadBoundRenderer.closed = 0
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["shader-deep", *arguments]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_report_run_reuses_fixed_inputs_and_one_directory(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            if len(self.requests) == 1:
                for path in (self.report / "reference.png", self.root / "baseline.glsl", self.root / "baseline.png"):
                    path.unlink()
            return self.render_then_finish(request)

        self.response = respond
        outcome = run_generation_from_report(
            self.report,
            "S1",
            "只复现圆形",
            background="透明",
            alternative=0,
            state=self.state,
            baseline_id="B7",
            asset_root=self.root,
            output_dir=self.options.output_dir,
        )
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(Path(outcome.selected_candidate.code_path).read_text(), BASE_CODE)
        self.assertEqual(ThreadBoundRenderer.renders, [(32, 24, 0.0)])
        self.assertEqual(list(self.options.output_dir.iterdir()), [outcome.run_dir])
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(Path(record["inputs"]["baseline"]["code_path"]).read_text(), BASE_CODE)
        self.assertEqual(self.state["candidates"]["B7"].code_path, "baseline.glsl")
        for request in self.requests:
            payload = task_payloads(request)[0]
            self.assertEqual(payload["generation_scheme"]["selected"]["choices"], {"F1": ["M2"]})
            self.assertEqual(payload["candidates"][0]["code"], BASE_CODE)

    def test_blocked_report_returns_outcome_and_cli_delivers_no_code(self) -> None:
        self.response = lambda _request: self.call("stop_generation", {"reason": "所选方案缺少形状条件", "next_action": "provide_input"})
        outcome = run_generation_from_report(self.report, "S1", "复现圆形", background="透明", output_dir=self.options.output_dir)
        self.assertEqual(outcome.stop_reason, "blocked")
        self.assertIsNone(outcome.selected_candidate)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(len(self.requests), 1)
        with patch("shader_deep.cli.generation.run_generation_from_report", return_value=outcome):
            status, stdout, stderr = self.cli("复现圆形", "--report", str(self.report), "--sketch", "S1", "--background", "透明")
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("Generation incomplete: blocked", stderr)
        self.assertIn(str(outcome.run_dir), stderr)

    def test_parameterized_baseline_is_fixed_and_reaches_both_context_modes(self) -> None:
        for memory in (False, True):
            with self.subTest(visual_memory=memory):
                self._verify_captured_parameters(memory=memory)

    def _verify_captured_parameters(self, *, memory: bool) -> None:
        code, preview, settings = (self.root / filename for filename in ("source.glsl", "source.png", "source.parameters.json"))
        code.write_text("uniform float uX;\n" + BASE_CODE, encoding="utf-8")
        preview.write_bytes((self.root / "baseline.png").read_bytes())
        spec = ParameterSpec.model_validate({"name": "uX", "type": "float", "default": 0.2, "min": 0.1, "max": 0.9})
        params = CandidateParameters(specs=(spec,), uniforms={"uX": 0.4})
        params.save(settings, code, preview, time=0.75)
        baseline = replace(self.state["candidates"]["B7"], id="B8", code_path=code.name, preview_path=preview.name, render_record_path=settings.name)

        def respond(request: dict[str, object]) -> dict[str, object]:
            if settings.exists():
                for source in (code, preview, settings):
                    source.unlink()
                if memory:
                    blocks = [block for message in request["messages"] if isinstance(message["content"], list) for block in message["content"]]
                    self.assertTrue(any('"uX": 0.4' in block.get("text", "") for block in blocks))
            if not memory:
                render = task_payloads(request)[0]["candidates"][0]["parameterized_render"]["render"]
                self.assertEqual(render, {"time": 0.75, "uniforms": {"uX": 0.4}})
            return self.render_then_finish(request)

        self.response = respond
        outcome = run_generation_from_report(
            self.report,
            "S1",
            "保留已知参数",
            background="透明",
            state=add_candidate(self.state, baseline),
            baseline_id="B8",
            asset_root=self.root,
            visual_memory=memory,
            output_dir=self.options.output_dir,
        )
        self.assertEqual(outcome.stop_reason, "completed")
        fixed = json.loads((outcome.run_dir / "run.json").read_text())["inputs"]["baseline"]
        self.assertEqual(read_candidate_parameters(Path(fixed["render_record_path"]), Path(fixed["code_path"]), Path(fixed["preview_path"])), params)

    def test_report_memory_and_model_budget_reach_prepared_session(self) -> None:
        def inspect_session(session: RenderSession) -> None:
            self.assertTrue(session.options.visual_memory)
            self.assertEqual(session.options.resolved_max_model_calls, 7)
            self.assertEqual((session.options.width, session.options.height), (32, 24))
            session.stop_reason = "blocked"

        with patch("shader_deep.workflows.generation._execute", side_effect=inspect_session):
            outcome = run_generation_from_report(
                self.report, "S1", "复现圆形", background="透明", visual_memory=True, max_model_calls=7, output_dir=self.options.output_dir
            )
        self.assertEqual(outcome.stop_reason, "blocked")
        self.assertEqual(outcome.model_calls, 0)
        self.assertEqual(self.requests, [])

    def test_invalid_report_model_budget_fails_before_preparing_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_model_calls"):
            run_generation_from_report(self.report, "S1", "复现圆形", background="透明", max_model_calls=0, output_dir=self.options.output_dir)
        self.assertFalse(self.options.output_dir.exists())

    def test_runtime_failure_preserves_error_diagnostic_and_existing_candidate(self) -> None:
        def fail_after_render(session: RenderSession) -> None:
            session.render_shader(BASE_CODE)
            msg = "model connection ended"
            raise RuntimeError(msg)

        with (
            patch("shader_deep.workflows.generation._execute", side_effect=fail_after_render),
            self.assertRaisesRegex(RuntimeError, "connection") as caught,
        ):
            run_generation_from_report(self.report, "S1", "复现圆形", background="白色", output_dir=self.options.output_dir)
        directory = next(self.options.output_dir.iterdir())
        record = json.loads((directory / "run.json").read_text())
        self.assertEqual(record["stop_reason"], "error")
        self.assertEqual(record["error"], "RuntimeError: model connection ended")
        self.assertEqual(len(record["blackboard"]["candidates"]), 1)
        self.assertTrue(list(directory.glob("*.glsl")))
        self.assertIn(str(directory), " ".join(caught.exception.__notes__))
        self.assertEqual(ThreadBoundRenderer.closed, 1)

    def test_report_cli_partial_dimensions_keep_reference_height(self) -> None:
        status, stdout, stderr = self.cli(
            "复现圆形",
            "--report",
            str(self.report),
            "--sketch",
            "S1",
            "--background",
            "白色",
            "--width",
            "48",
            "--output-dir",
            str(self.options.output_dir),
        )
        self.assertEqual((status, stdout), (0, BASE_CODE + "\n"))
        self.assertEqual(ThreadBoundRenderer.renders, [(48, 24, 0.0)])
        self.assertIn("Preview:", stderr)

    def test_renderer_close_failure_is_recorded_after_a_selected_candidate(self) -> None:
        with (
            patch.object(ThreadBoundRenderer, "close", side_effect=RuntimeError("browser close failed")),
            self.assertRaisesRegex(RuntimeError, "browser close failed") as caught,
        ):
            run_generation_from_report(self.report, "S1", "复现圆形", background="白色", output_dir=self.options.output_dir)
        directory = next(self.options.output_dir.iterdir())
        record = json.loads((directory / "run.json").read_text())
        self.assertEqual(record["stop_reason"], "error")
        self.assertEqual(record["error"], "RuntimeError: browser close failed")
        self.assertIsNotNone(record["selected_candidate_id"])
        self.assertIn(str(directory), " ".join(caught.exception.__notes__))
        self.assertFalse(any(thread.name.startswith("shader-render") for thread in threading.enumerate()))

    def test_mixed_cli_mode_and_invalid_dimensions_fail_before_execution(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.cli(str(self.root / "reference.PNG"), "复现圆形", "--report", str(self.report), "--sketch", "S1", "--background", "白色")
        self.assertEqual(caught.exception.code, 2)
        status, stdout, stderr = self.cli("复现圆形", "--report", str(self.report), "--sketch", "S1", "--background", "白色", "--width", "0")
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("positive integer", stderr)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.options.output_dir.exists())
