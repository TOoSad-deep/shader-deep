"""报告入口复用生成闭环、固定目录和命令行交付语义."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.api import run_generation_from_report
from shader_deep.cli.generation import main
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
