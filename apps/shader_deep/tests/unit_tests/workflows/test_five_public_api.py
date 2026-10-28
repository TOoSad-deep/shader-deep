"""公开 Python/CLI 入口通过原生图交付可读取的五库包."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Never
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.api import read_report_package, read_sketch, run_analysis, run_analysis_task
from shader_deep.cli.analysis import main
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.workflows.analysis.pipeline import execute_five_analysis
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, context

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


class InterruptedIntegrationModel(NativeRoutingModel):
    def _integrate(self, messages: list[BaseMessage], payload: dict[str, object]) -> Never:
        del messages, payload
        raise KeyboardInterrupt


class FivePublicApiTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.reference = self.root / "pink.png"
        Image.new("RGB", (8, 8), (255, 100, 150)).save(self.reference)
        self.options = AnalysisOptions(output_dir=self.root / "runs", max_request_retries=0)

    def test_default_api_delivers_consumer_compatible_package(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel(merge=True)):
            outcome = run_analysis(self.reference, "只分析粉色本体", options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.summary_result.analysis_protocol, "five_libraries_v1")
        self.assertIsNone(outcome.summary_result.analysis_detail)
        manifest, libraries = read_report_package(outcome.report_dir)
        self.assertEqual(manifest.reference.path, "reference.png")
        self.assertEqual(len(libraries.sketches), 3)
        self.assertEqual(len(libraries.mechanisms), 1)
        self.assertEqual(read_sketch(outcome.report_dir, "S2")["selected"]["choices"], {"F1": ["M1"]})
        self.assertEqual((outcome.report_dir / "reference.png").read_bytes(), self.reference.read_bytes())
        record = json.loads((outcome.run_dir / "manifest.json").read_text())
        self.assertTrue(record["closed"])
        self.assertEqual(record["versions"], ["V0", "V1"])

    def test_task_requirements_are_bound_in_actual_worker_requests(self) -> None:
        state = add_target(new_blackboard(), TargetRecord(version="unused", request="其他目标要求", reference_path=str(self.reference)))
        target = TargetRecord(
            version="T1",
            request="复现粉色圆形",
            reference_path=str(self.reference),
            constraints=("只使用二维遮罩", "保持透明背景"),
            protected_features=("双亮斑", "柔和边缘"),
        )
        state = add_target(state, target)
        task = TaskRecord(id="inspect", role="analysis", target_version="T1", objective="分析主体")
        state = add_task(state, task)
        model = NativeRoutingModel()
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=model):
            outcome = run_analysis_task(state, task.id, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        inputs = [context(messages) for messages in model.requests if "direction" in context(messages)]
        self.assertEqual(len({payload["direction"] for payload in inputs}), 3)
        for payload in inputs:
            for required in (target.request, task.objective, *target.constraints, *target.protected_features):
                self.assertIn(required, payload["user_request"])
            self.assertNotIn("其他目标要求", payload["user_request"])
        self.assertEqual(state["targets"]["T1"], target)
        self.assertEqual(state["tasks"][task.id], task)

    def test_integration_failure_delivers_v0_with_gap(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel(integration_idle=True)):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        manifest, library = read_report_package(outcome.report_dir)
        self.assertEqual(manifest.status, "partial")
        self.assertTrue(any("整合未完成" in item.description for item in manifest.gaps))
        self.assertEqual(len(library.features), 3)
        self.assertEqual(json.loads((outcome.run_dir / "run.json").read_text())["selected_version"], "V0")

    def test_no_valid_baseline_does_not_publish_empty_package(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel(invalid_target=True)):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        self.assertEqual(outcome.stop_reason, "failed")
        self.assertIsNone(outcome.report_dir)
        self.assertIsNone(outcome.summary_result)
        self.assertFalse((outcome.run_dir / "report").exists())

    def test_invalid_or_unsupported_options_fail_before_model_and_output_creation(self) -> None:
        for overrides, error in (
            ({"max_tasks": 4}, ValueError),
            ({"max_main_calls": 2}, TypeError),
            ({"max_context_tokens": 4096}, TypeError),
            ({"max_repeated_no_progress": 3}, TypeError),
        ):
            with self.subTest(overrides=overrides), patch("shader_deep.workflows.analysis.entry.build_model") as model, self.assertRaises(error):
                run_analysis(self.reference, "分析圆形", options=replace(self.options, **overrides))
            model.assert_not_called()
        self.assertFalse((self.root / "runs").exists())

    def test_cli_returns_short_index_and_respects_log_level(self) -> None:
        for level in ("INFO", "ERROR"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(level=level),
                patch.object(
                    sys,
                    "argv",
                    ["shader-deep-analyze", str(self.reference), "分析圆形", "--output-dir", str(self.root / "cli"), "--log-level", level],
                ),
                patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel()),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(main(), 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["status"], "completed")
            self.assertTrue((Path(output["report_dir"]) / "mechanisms.json").is_file())
            self.assertIsNone(output["result"]["analysis_detail"])
            self.assertIn("README.md", stderr.getvalue())
            self.assertIn("报告包已发布", (Path(output["run_dir"]) / "analysis.log").read_text())
            if level == "ERROR":
                self.assertNotIn("原生分析运行开始", stderr.getvalue())

    def test_worker_failure_preserves_other_sources(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel(permanent_direction="检查色彩")):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        manifest, libraries = read_report_package(outcome.report_dir)
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(len(libraries.sketches), 2)
        self.assertTrue(any("model connection failed" in gap.description for gap in manifest.gaps))

    def test_interrupt_preserves_valid_partial_package(self) -> None:
        with (
            patch("shader_deep.workflows.analysis.entry.build_model", return_value=InterruptedIntegrationModel()),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_analysis(self.reference, "分析圆形", options=self.options)
        directory = next((self.root / "runs").iterdir())
        self.assertEqual(read_report_package(directory / "report")[0].status, "partial")
        self.assertEqual(json.loads((directory / "run.json").read_text())["stop_reason"], "partial")

    def test_existing_run_is_rejected_without_overwriting_results(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=NativeRoutingModel()):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        snapshot = (outcome.run_dir / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "不续跑"):
            execute_five_analysis("分析圆形", "data:image/png;base64,AA==", self.options, outcome.run_dir, model=NativeRoutingModel())
        self.assertEqual((outcome.run_dir / "manifest.json").read_bytes(), snapshot)
