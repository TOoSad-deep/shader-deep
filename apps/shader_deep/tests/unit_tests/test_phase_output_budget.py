"""验证阶段配置优先级及真实序列化请求与本地预算的一致性."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep import analysis_cli
from shader_deep.analysis.config import apply_analysis_overrides, load_analysis_options, resolve_phase_options
from shader_deep.analysis.events import EventLog
from shader_deep.analysis.exploration import OutlineElement, VisualOutline
from shader_deep.analysis.exploration_worker import ExplorationOutcome, run_exploration
from shader_deep.analysis.request_budget import estimated_tokens
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from shader_deep.blackboard import new_blackboard
from tests.unit_tests._generation_fixture import GenerationFixture


class PhaseOutputConfigurationTests(TestCase):
    def test_precedence_retains_original_configuration_and_sources(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("max_output_tokens: 60000\noutline_max_output_tokens: 8000\nworker_max_output_tokens: 40000\n", encoding="utf-8")
            options = load_analysis_options(path)
            self.assertEqual(resolve_phase_options(options, "outline")[0].max_output_tokens, 8000)
            self.assertEqual(resolve_phase_options(options, "outline")[1], "stage_yaml")
            self.assertEqual(resolve_phase_options(options, "integration")[1], "yaml")
            overridden = apply_analysis_overrides(options, {"max_output_tokens": 20000, "worker_max_output_tokens": 30000})
            for phase, value, source in (("outline", 20000, "cli"), ("integration", 20000, "cli"), ("worker", 30000, "stage_cli")):
                with self.subTest(phase=phase):
                    effective, origin = resolve_phase_options(overridden, phase)
                    self.assertEqual((effective.max_output_tokens, origin), (value, source))
                    self.assertEqual(estimated_tokens(0, 0, effective), value + effective.request_token_margin)
            self.assertEqual(overridden.outline_max_output_tokens, 8000)
            self.assertEqual(options.max_output_tokens, 60000)
            json.dumps(asdict(overridden))

    def test_unset_defaults_and_programmatic_values(self) -> None:
        defaults, source = resolve_phase_options(AnalysisOptions(), "outline")
        self.assertEqual((defaults.max_output_tokens, source), (16384, "default"))
        self.assertIsNone(defaults.outline_max_output_tokens)
        options = AnalysisOptions(max_output_tokens=10000, worker_max_output_tokens=12000)
        self.assertEqual(resolve_phase_options(options, "outline")[1], "options")
        self.assertEqual(resolve_phase_options(options, "worker")[1], "stage_options")

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
                patch("shader_deep.analysis_cli.run_analysis", return_value=outcome) as run,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(analysis_cli.main(), 1)
            options = run.call_args.kwargs["options"]
            self.assertEqual(resolve_phase_options(options, "outline")[0].max_output_tokens, 16000)
            self.assertEqual(resolve_phase_options(options, "worker")[0].max_output_tokens, 32000)


class WorkerOutputBudgetTests(GenerationFixture):
    def run_worker(self, options: AnalysisOptions) -> ExplorationOutcome:
        outline = VisualOutline(elements=(OutlineElement(id="E1", name="背景", scope="全图", salient_features=("纯色",)),), relations=())
        return run_exploration(
            "分析", outline, "外观", "data:image/png;base64,image", options=options, config={}, task_id="worker-1", event_log=EventLog(self.root)
        )

    def test_actual_request_and_record_use_effective_worker_budget(self) -> None:
        self.response = lambda _: self.call("stop_exploration", {"reason": "test stop"})
        outcome = self.run_worker(AnalysisOptions(max_output_tokens=70000, worker_max_output_tokens=8000))
        self.assertEqual(outcome.execution.model_calls, 1)
        self.assertEqual(self.requests[0].get("max_completion_tokens", self.requests[0].get("max_tokens")), 8000)
        records = [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]
        budget = next(item["details"] for item in records if item["event"] == "phase_output_budget")
        self.assertEqual(budget, {"phase": "worker", "max_output_tokens": 8000, "source": "stage_options"})

    def test_deepseek_legacy_payload_uses_the_same_effective_budget(self) -> None:
        self.response = lambda _: self.call("stop_exploration", {"reason": "test stop"})
        with patch.dict(os.environ, {"MICU_MODEL": "deepseek-fixture"}):
            outcome = self.run_worker(AnalysisOptions(max_output_tokens=70000, worker_max_output_tokens=9000))
        self.assertEqual(outcome.execution.model_calls, 1)
        self.assertEqual(self.requests[0]["max_tokens"], 9000)
        self.assertNotIn("max_completion_tokens", self.requests[0])

    def test_worker_budget_rejects_before_network_send(self) -> None:
        outcome = self.run_worker(AnalysisOptions(max_context_tokens=1000, worker_max_output_tokens=2000))
        self.assertEqual(self.requests, [])
        self.assertIn("input_budget_exceeded", outcome.error)
