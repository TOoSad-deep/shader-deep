"""评审 CLI 的严格任务输入和交付退出语义."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from shader_deep.cli.review import main
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.review.contracts import ReviewOutcome
from tests.unit_tests.fixtures._review_fixture import review_request
from tests.unit_tests.fixtures._review_model import ReviewModel


class ReviewCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task = self.root / "task.json"
        self.task.write_text(review_request().model_dump_json(), encoding="utf-8")

    def test_cli_runs_native_graph_and_publishes_readable_report(self) -> None:
        reference, rendered = self.root / "reference.png", self.root / "rendered.png"
        with Image.new("RGB", (8, 6), "red") as image:
            image.save(reference)
            image.save(rendered)
        output = io.StringIO()
        arguments = ["review", str(reference), str(rendered), "--task", str(self.task), "--output-dir", str(self.root / "runs")]
        with (
            patch("sys.argv", arguments),
            patch("shader_deep.workflows.review.entry.build_model", return_value=ReviewModel()),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        index = json.loads(output.getvalue())
        package = read_review_package(Path(index["report_dir"]))
        self.assertEqual(index["status"], "completed")
        self.assertEqual(package.report.verdict, index["verdict"])
        self.assertEqual(package.source.request, review_request())

    def test_visual_verdict_does_not_change_success_exit_code(self) -> None:
        config = self.root / "review.yaml"
        config.write_text("max_model_calls: 7\noutput_dir: outputs\n", encoding="utf-8")
        arguments = ["review", "reference.png", "rendered.png", "--task", str(self.task), "--config", str(config), "--max-model-calls", "2"]
        for verdict in ("needs_revision", "inconclusive", "no_major_issue_observed"):
            outcome = ReviewOutcome(run_dir=self.root, report_dir=self.root / "report", status="completed", verdict=verdict)
            output = io.StringIO()
            with (
                self.subTest(verdict=verdict),
                patch("sys.argv", arguments),
                patch("shader_deep.cli.review.run_review", return_value=outcome) as run,
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())["verdict"], verdict)
            self.assertEqual(run.call_args.kwargs["options"].max_model_calls, 2)
            self.assertEqual(run.call_args.kwargs["options"].output_dir, self.root.resolve() / "outputs")

    def test_duplicate_json_key_is_rejected_before_model_execution(self) -> None:
        self.task.write_text('{"scope":"local","scope":"scene"}', encoding="utf-8")
        with (
            patch("sys.argv", ["review", "r.png", "c.png", "--task", str(self.task)]),
            patch("shader_deep.cli.review.run_review") as run,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(), 2)
        run.assert_not_called()

    def test_directory_and_input_copy_failures_emit_failure_index(self) -> None:
        reference = self.root / "reference.png"
        with Image.new("RGB", (2, 2)) as image:
            image.save(reference)
        argv = ["review", str(reference), str(reference), "--task", str(self.task), "--output-dir", str(self.root)]
        for operation in (
            "shader_deep.workflows.review.entry.create_run_directory",
            "shader_deep.infrastructure.storage.review_package._write_contents",
        ):
            output = io.StringIO()
            with (
                self.subTest(operation=operation),
                patch("sys.argv", argv),
                patch(operation, side_effect=OSError("disk full")),
                redirect_stderr(io.StringIO()),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "failed")
            self.assertIn("disk full", result["error"])

    def test_missing_input_png_is_a_usage_error(self) -> None:
        output = io.StringIO()
        argv = ["review", str(self.root / "absent.png"), "c.png", "--task", str(self.task), "--output-dir", str(self.root)]
        with patch("sys.argv", argv), redirect_stderr(io.StringIO()), redirect_stdout(output):
            self.assertEqual(main(), 2)
        self.assertEqual(output.getvalue(), "")

    def test_blocked_uses_failure_exit_code(self) -> None:
        outcome = ReviewOutcome(run_dir=self.root, report_dir=None, status="blocked", error="no_submission")
        with (
            patch("sys.argv", ["review", "r.png", "c.png", "--task", str(self.task)]),
            patch("shader_deep.cli.review.run_review", return_value=outcome),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 1)
