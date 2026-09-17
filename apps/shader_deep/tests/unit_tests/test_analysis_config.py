"""验证 YAML 参数校验、路径语义及 CLI 覆盖优先级, 不请求模型服务."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep import analysis_cli
from shader_deep.analysis import config as analysis_config
from shader_deep.analysis.config import load_analysis_options
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from shader_deep.blackboard import new_blackboard


class AnalysisConfigTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.config = self.root / "config.yaml"
        default = patch("shader_deep.analysis.config.DEFAULT_CONFIG", self.config)
        default.start()
        self.addCleanup(default.stop)

    def test_default_discovery_partial_config_and_relative_output(self) -> None:
        self.assertEqual(load_analysis_options(), AnalysisOptions())
        self.config.write_text("max_output_tokens: 32768\nmax_worker_calls: 5\noutput_dir: artifacts\n", encoding="utf-8")
        options = load_analysis_options()
        self.assertEqual(options.max_output_tokens, 32768)
        self.assertEqual(options.max_worker_calls, 5)
        self.assertEqual(options.max_main_calls, 0)
        self.assertEqual(options.output_dir, self.root / "artifacts")

    def test_explicit_missing_config_does_not_fall_back(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_analysis_options(self.config)

    def test_bundled_config_is_independent_of_working_directory(self) -> None:
        bundled = Path(analysis_config.__file__).with_name("config.yaml")
        self.assertTrue(bundled.is_file())
        # 当前目录的同名文件不能覆盖模块配置, 也不再自动读取旧位置的 analysis.yaml.
        self.config.write_text("max_worker_calls: 7\n", encoding="utf-8")
        (self.root / "analysis.yaml").write_text("max_worker_calls: 7\n", encoding="utf-8")
        with patch("shader_deep.analysis.config.DEFAULT_CONFIG", bundled):
            expected = load_analysis_options()
            self.assertEqual(expected.max_main_calls, 0)
            self.assertEqual(expected.max_worker_calls, 0)
            with contextlib.chdir(self.root):
                self.assertEqual(load_analysis_options(), expected)
                self.assertIsNone(load_analysis_options().output_dir)

    def test_invalid_config_is_rejected(self) -> None:
        cases = (
            "[1, 2]",
            "max_output_token: 100",
            "max_tasks: 1",
            "max_worker_calls: true",
            "max_worker_calls: -1",
            'max_output_tokens: "32768"',
            "max_parallel: 1.5",
            "max_main_calls: null",
            "max_main_calls: -1",
            "max_main_calls: false",
            "max_repeated_no_progress: -1",
            "max_repeated_no_progress: true",
            "max_measurements: 0",
            "max_request_retries: -1",
            "request_timeout_seconds: 0",
            'stream_model_responses: "false"',
            "output_dir: 123",
            'output_dir: " "',
            "max_tasks: [",
            "!!python/object/apply:builtins.dict []",
        )
        for content in cases:
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                with self.assertRaises((TypeError, ValueError)):
                    load_analysis_options(self.config)

    def test_empty_config_and_null_output_use_defaults(self) -> None:
        for content in ("", "{}", "output_dir: null"):
            with self.subTest(content=content):
                self.config.write_text(content, encoding="utf-8")
                self.assertEqual(load_analysis_options(self.config), AnalysisOptions())

    def test_cli_explicit_config_and_overrides_preserve_false_and_zero(self) -> None:
        self.config.write_text("max_worker_calls: 99\n", encoding="utf-8")
        explicit = self.root / "custom.yaml"
        explicit.write_text(
            "max_worker_calls: 5\nmax_output_tokens: 32768\nstream_model_responses: true\nmax_request_retries: 2\noutput_dir: artifacts\n",
            encoding="utf-8",
        )
        arguments = [
            "shader-deep-analyze",
            "reference.png",
            "Analyze",
            "--config",
            str(explicit),
            "--max-output-tokens",
            "24576",
            "--no-stream-model-responses",
            "--max-request-retries",
            "0",
            "--max-main-calls",
            "0",
            "--max-worker-calls",
            "0",
            "--max-repeated-no-progress",
            "3",
            "--output-dir",
            "cli-runs",
        ]
        outcome = AnalysisOutcome(state=new_blackboard(), task_id="A1", summary_result=None, run_dir=self.root, stop_reason="model_limit")
        with (
            patch.object(sys, "argv", arguments),
            patch("shader_deep.analysis_cli.run_analysis", return_value=outcome) as run,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(analysis_cli.main(), 1)
        options = run.call_args.kwargs["options"]
        self.assertEqual(options.max_worker_calls, 0)
        self.assertEqual(options.max_output_tokens, 24576)
        self.assertFalse(options.stream_model_responses)
        self.assertEqual(options.max_request_retries, 0)
        self.assertEqual(options.max_main_calls, 0)
        self.assertEqual(options.max_repeated_no_progress, 3)
        self.assertEqual(options.output_dir, Path("cli-runs"))

    def test_invalid_cli_config_fails_before_model_invocation(self) -> None:
        self.config.write_text("max_worker_calls: -1", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["shader-deep-analyze", "reference.png", "Analyze"]),
            patch("shader_deep.analysis_cli.run_analysis") as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(analysis_cli.main(), 2)
        run.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("max_worker_calls", stderr.getvalue())
