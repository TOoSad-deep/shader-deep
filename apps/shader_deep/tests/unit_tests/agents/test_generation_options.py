"""验证独立模型预算、显式记忆开关及命令行的两种输入模式."""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest import TestCase
from unittest.mock import patch

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import new_blackboard
from shader_deep.domain.tasks import CandidateRecord


class GenerationOptionsTests(TestCase):
    def test_default_model_budget_preserves_legacy_and_supports_memory(self) -> None:
        self.assertEqual(GenerationOptions(max_attempts=9).resolved_max_model_calls, 11)
        self.assertEqual(GenerationOptions(max_attempts=9, visual_memory=True).resolved_max_model_calls, 12)

    def test_explicit_model_budget_is_independent_of_render_budget(self) -> None:
        for enabled in (False, True):
            with self.subTest(visual_memory=enabled):
                options = GenerationOptions(max_attempts=9, visual_memory=enabled, max_model_calls=7)
                self.assertEqual(options.resolved_max_model_calls, 7)
                self.assertEqual(options.max_attempts, 9)

    def test_model_budget_rejects_boolean_non_integer_and_nonpositive(self) -> None:
        for value in (True, False, 0, -1, 2.5, "4"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_model_calls must be a positive integer"):
                GenerationOptions(max_model_calls=cast("int", value))

    def test_memory_flag_requires_actual_boolean(self) -> None:
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "visual_memory must be a boolean"):
                GenerationOptions(visual_memory=cast("bool", value))


class GenerationOptionsCliTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        code = self.root / "candidate.glsl"
        code.write_text("rendered shader", encoding="utf-8")
        self.outcome = GenerationOutcome(
            state=new_blackboard(),
            run_dir=self.root,
            selected_candidate=CandidateRecord(id="candidate", task_id="task", code_path=str(code), preview_path=str(self.root / "preview.png")),
            stop_reason="completed",
            attempts=1,
            model_calls=2,
        )

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["shader-deep", *arguments]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = importlib.import_module("shader_deep.cli.generation").main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_png_flags_reach_options_and_stdout_remains_shader_only(self) -> None:
        with patch("shader_deep.cli.generation.run_shader", return_value=self.outcome) as run:
            status, stdout, stderr = self.cli("input.png", "目标", "--visual-memory", "--max-model-calls", "7")
        self.assertEqual((status, stdout), (0, "rendered shader\n"))
        self.assertIn("Preview:", stderr)
        options = run.call_args.kwargs["options"]
        self.assertIsInstance(options, GenerationOptions)
        self.assertTrue(options.visual_memory)
        self.assertEqual(options.resolved_max_model_calls, 7)

    def test_report_flags_reach_public_entry_and_stdout_remains_shader_only(self) -> None:
        with patch("shader_deep.cli.generation.run_generation_from_report", return_value=self.outcome) as run:
            status, stdout, _ = self.cli(
                "目标", "--report", "report", "--sketch", "S1", "--background", "透明", "--visual-memory", "--max-model-calls", "8"
            )
        self.assertEqual((status, stdout), (0, "rendered shader\n"))
        self.assertTrue(run.call_args.kwargs["visual_memory"])
        self.assertEqual(run.call_args.kwargs["max_model_calls"], 8)

    def test_absent_flags_preserve_legacy_mode_and_budget(self) -> None:
        with patch("shader_deep.cli.generation.run_shader", return_value=self.outcome) as run:
            status, _, _ = self.cli("input.png", "目标", "--max-attempts", "5")
        self.assertEqual(status, 0)
        options = run.call_args.kwargs["options"]
        self.assertFalse(options.visual_memory)
        self.assertIsNone(options.max_model_calls)
        self.assertEqual(options.resolved_max_model_calls, 7)

    def test_invalid_budget_fails_before_png_execution(self) -> None:
        with patch("shader_deep.cli.generation.run_shader") as run:
            status, stdout, stderr = self.cli("input.png", "目标", "--max-model-calls", "0")
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("max_model_calls", stderr)
        run.assert_not_called()

    def test_planning_mode_rejects_memory_before_execution(self) -> None:
        with (
            patch("shader_deep.cli.generation.run_generation_from_planning") as run,
            self.assertRaises(SystemExit) as caught,
        ):
            self.cli("--planning-report", "report", "--sketch", "S1", "--visual-memory")
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()

    def test_planning_mode_forwards_independent_model_budget(self) -> None:
        with (
            patch("shader_deep.cli.generation.read_planning_package") as read,
            patch("shader_deep.cli.generation.run_generation_from_planning", return_value=self.outcome) as run,
        ):
            read.return_value.source.constraints = GenerationOptions(width=40, height=30, time=0.5)
            status, stdout, _ = self.cli("--planning-report", "report", "--sketch", "S1", "--max-model-calls", "7")
        self.assertEqual((status, stdout), (0, "rendered shader\n"))
        options = run.call_args.kwargs["options"]
        self.assertFalse(options.visual_memory)
        self.assertEqual(options.resolved_max_model_calls, 7)
        self.assertEqual((options.width, options.height, options.time), (40, 30, 0.5))
