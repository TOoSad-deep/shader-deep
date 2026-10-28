"""阶段配置的可观察优先级与 CLI 覆盖合同."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep.cli import analysis as analysis_cli
from shader_deep.domain.blackboard import new_blackboard
from shader_deep.infrastructure.llm.options import resolve_role_options
from shader_deep.workflows.configuration import apply_analysis_overrides, load_analysis_options
from shader_deep.workflows.options import AnalysisOptions
from shader_deep.workflows.outcomes import AnalysisOutcome


class PhaseOutputConfigurationTests(TestCase):
    def test_precedence_retains_original_configuration_and_sources(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("max_output_tokens: 60000\noutline_max_output_tokens: 8000\nworker_max_output_tokens: 40000\n", encoding="utf-8")
            options = load_analysis_options(path)
            self.assertEqual(resolve_role_options(options, "outline").max_output_tokens, 8000)
            self.assertEqual(resolve_role_options(options, "integration").max_output_tokens, 60000)
            overridden = apply_analysis_overrides(options, {"max_output_tokens": 20000, "worker_max_output_tokens": 30000})
            for phase, value in (("outline", 20000), ("integration", 20000), ("worker", 30000)):
                with self.subTest(phase=phase):
                    self.assertEqual(resolve_role_options(overridden, phase).max_output_tokens, value)
            self.assertEqual(overridden.outline_max_output_tokens, 8000)
            self.assertEqual(options.max_output_tokens, 60000)
            json.dumps(asdict(overridden))

    def test_unset_defaults_and_programmatic_values(self) -> None:
        defaults = resolve_role_options(AnalysisOptions(), "outline")
        self.assertEqual(defaults.max_output_tokens, 16384)
        options = AnalysisOptions(max_output_tokens=10000, worker_max_output_tokens=12000)
        self.assertEqual(resolve_role_options(options, "outline").max_output_tokens, 10000)
        self.assertEqual(resolve_role_options(options, "worker").max_output_tokens, 12000)

    def test_invalid_phase_values_and_internal_source_injection_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for name in ("outline_max_output_tokens", "integration_max_output_tokens", "worker_max_output_tokens"):
                for value in (0, -1, True, "8000", 1.5):
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        replace(AnalysisOptions(), **{name: value})
            path.write_text("_output_token_sources: []", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_analysis_options(path)
            with self.assertRaises(ValueError):
                apply_analysis_overrides(AnalysisOptions(), {"_output_token_sources": ()})

    def test_cli_generic_override_and_explicit_stage_override(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text("outline_max_output_tokens: 8000\nworker_max_output_tokens: 40000\n", encoding="utf-8")
            args = ["analyze", "input.png", "inspect", "--config", str(path), "--max-output-tokens", "16000", "--worker-max-output-tokens", "32000"]
            outcome = AnalysisOutcome(state=new_blackboard(), task_id="A1", summary_result=None, run_dir=root, stop_reason="partial")
            with (
                patch.object(sys, "argv", args),
                patch("shader_deep.cli.analysis.run_analysis", return_value=outcome) as run,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(analysis_cli.main(), 1)
            options = run.call_args.kwargs["options"]
            self.assertEqual(resolve_role_options(options, "outline").max_output_tokens, 16000)
            self.assertEqual(resolve_role_options(options, "worker").max_output_tokens, 32000)
