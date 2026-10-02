"""默认公开入口与 CLI 经真实角色链路交付五库文件包."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.api import read_report_package, read_sketch, run_analysis, run_analysis_task
from shader_deep.cli.analysis import main
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.analysis import _delivery
from shader_deep.workflows.five_analysis import FiveAnalysisResult, execute_five_analysis
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.workflows.test_five_analysis import RoutingFakeModel, context


class FivePublicApiTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "pink.png"
        Image.new("RGB", (8, 8), (255, 100, 150)).save(self.reference)
        self.options = AnalysisOptions(output_dir=self.root / "runs", max_request_retries=0)

    def test_default_public_api_runs_full_five_library_path(self) -> None:
        with patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel(merge=True)):
            outcome = run_analysis(self.reference, "只分析粉色本体", options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.summary_result.analysis_protocol, "five_libraries_v1")
        self.assertIsNone(outcome.summary_result.analysis_detail)
        manifest, libraries = read_report_package(outcome.report_dir)
        self.assertEqual(manifest.reference.path, "reference.png")
        self.assertEqual(len(libraries.sketches), 3)
        self.assertEqual(len(libraries.mechanisms), 1)
        self.assertEqual(read_sketch(outcome.report_dir, "S2")["selected"]["choices"], {"F1": ["M1"]})
        commit = json.loads((outcome.run_dir / "commit.json").read_text())
        self.assertTrue(commit["sealed"])
        self.assertEqual(commit["package_path"], "report")
        self.assertTrue({"V0", "V1"} <= set(commit["versions"]))
        self.assertEqual((outcome.report_dir / "reference.png").read_bytes(), self.reference.read_bytes())

    def test_task_entry_preserves_bound_target_requirements_in_actual_requests(self) -> None:
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
        model = RoutingFakeModel()
        with patch("shader_deep.workflows.analysis.build_model", return_value=model):
            outcome = run_analysis_task(state, task.id, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        inputs = [context(messages) for messages in model.requests if "user_request" in context(messages)]
        self.assertTrue(inputs)
        self.assertEqual(len([payload for payload in inputs if "direction" in payload]), 3)
        for payload in inputs:
            request = payload["user_request"]
            for required in (target.request, task.objective, *target.constraints, *target.protected_features):
                self.assertIn(required, request)
            self.assertNotIn("其他目标要求", request)
        commit = json.loads((outcome.run_dir / "commit.json").read_text())
        self.assertEqual(commit["tasks"]["main"]["payload"]["user_request"], inputs[0]["user_request"])
        self.assertEqual(state["targets"]["T1"], target)
        self.assertEqual(state["tasks"][task.id], task)

    def test_simple_entry_keeps_request_once_when_objective_matches(self) -> None:
        prompt = "分析粉色圆形"
        model = RoutingFakeModel()
        with patch("shader_deep.workflows.analysis.build_model", return_value=model):
            outcome = run_analysis(self.reference, prompt, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        inputs = [context(messages) for messages in model.requests if "user_request" in context(messages)]
        self.assertTrue(inputs)
        self.assertEqual(len([payload for payload in inputs if "direction" in payload]), 3)
        self.assertTrue(all(payload["user_request"] == prompt for payload in inputs))

    def test_integration_failure_delivers_v0_and_global_gap(self) -> None:
        with patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel(integration_idle=True)):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        manifest, library = read_report_package(outcome.report_dir)
        self.assertEqual(manifest.status, "partial")
        self.assertIn("整合未完成", manifest.gaps[-1].description)
        self.assertEqual(len(library.features), 3)
        projection = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(projection["selected_version"], "V0")
        self.assertNotIn("V1", projection["execution"]["versions"])

    def test_no_valid_baseline_produces_failure_without_empty_package(self) -> None:
        with patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel(invalid_target=True)):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        self.assertEqual(outcome.stop_reason, "failed")
        self.assertIsNone(outcome.report_dir)
        self.assertIsNone(outcome.summary_result)
        self.assertFalse((outcome.run_dir / "report").exists())
        self.assertTrue(json.loads((outcome.run_dir / "commit.json").read_text())["sealed"])

    def test_fourth_perspective_is_rejected_before_model_or_run_creation(self) -> None:
        with patch("shader_deep.workflows.analysis.build_model") as model, self.assertRaises(ValueError):
            run_analysis(self.reference, "分析圆形", options=AnalysisOptions(max_tasks=4, output_dir=self.root / "runs"))
        model.assert_not_called()
        self.assertFalse((self.root / "runs").exists())

    def test_cli_stdout_is_a_short_index_to_the_complete_package(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["shader-deep-analyze", str(self.reference), "分析圆形", "--output-dir", str(self.root / "cli")]),
            patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel()),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "completed")
        self.assertTrue((Path(output["report_dir"]) / "mechanisms.json").is_file())
        self.assertIsNone(output["result"]["analysis_detail"])
        self.assertIn("README.md", stderr.getvalue())
        self.assertIn("元素 E1: 圆形", stderr.getvalue())
        self.assertIn("features F1: 目标的圆形轮廓", stderr.getvalue())
        self.assertIn("报告包已发布并封存", stderr.getvalue())
        log = (Path(output["run_dir"]) / "analysis.log").read_text()
        self.assertIn("完整业务提交与回执", log)
        for task_id in ("main", "exploration-1", "exploration-2", "exploration-3", "integration"):
            self.assertIn(f"/{task_id} attempt=", log)

    def test_cli_error_level_keeps_complete_file_log_and_clean_stdout(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["shader-deep-analyze", str(self.reference), "分析圆形", "--output-dir", str(self.root / "quiet"), "--log-level", "ERROR"],
            ),
            patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel()),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)
        output = json.loads(stdout.getvalue())
        self.assertNotIn("元素 E1", stderr.getvalue())
        log = (Path(output["run_dir"]) / "analysis.log").read_text()
        self.assertIn("元素 E1: 圆形", log)
        self.assertIn("完整五库版本", log)
        self.assertEqual(output["status"], "completed")

    def test_failed_worker_is_visible_without_discarding_other_results(self) -> None:
        with patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel(permanent_direction="检查色彩")):
            outcome = run_analysis(self.reference, "分析圆形", options=self.options)
        self.assertEqual(outcome.stop_reason, "partial")
        log = (outcome.run_dir / "analysis.log").read_text()
        self.assertIn("任务执行失败", log)
        self.assertIn("model connection failed", log)
        self.assertIn("/exploration-2 attempt=", log)
        self.assertIn("五库版本已发布", log)
        self.assertIn('"status": "partial"', log)

    def test_keyboard_interrupt_preserves_partial_package_and_projection(self) -> None:
        with (
            patch("shader_deep.workflows.analysis.build_model", return_value=RoutingFakeModel()),
            patch("shader_deep.workflows.five_analysis.run_integration", side_effect=KeyboardInterrupt()),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_analysis(self.reference, "分析圆形", options=self.options)
        directory = next((self.root / "runs").iterdir())
        manifest, _ = read_report_package(directory / "report")
        self.assertEqual(manifest.status, "partial")
        self.assertTrue(json.loads((directory / "commit.json").read_text())["sealed"])
        self.assertEqual(json.loads((directory / "run.json").read_text())["stop_reason"], "partial")

    def test_prepared_partial_package_recovers_after_seal_failure_without_rewrite(self) -> None:
        directory = self.root / "recovery"
        directory.mkdir()
        reference = self.reference.read_bytes()
        (directory / "reference.png").write_bytes(reference)
        reference_url = "data:image/png;base64," + base64.b64encode(reference).decode("ascii")

        def deliver(result: FiveAnalysisResult, store: TaskStore) -> None:
            _delivery(directory, reference, result, store)

        with (
            patch("shader_deep.workflows.five_analysis.run_integration", side_effect=KeyboardInterrupt()),
            patch.object(TaskStore, "seal", side_effect=OSError("模拟包写齐后封存失败")),
            self.assertRaises(OSError),
        ):
            execute_five_analysis("分析圆形", reference_url, self.options, directory, model=RoutingFakeModel(), on_delivery=deliver)
        previous = {path.name: path.read_bytes() for path in (directory / "report").iterdir()}
        model = RoutingFakeModel()
        result = execute_five_analysis("分析圆形", reference_url, self.options, directory, model=model, on_delivery=deliver)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.selected_version, "V0")
        self.assertEqual(model.requests, [])
        self.assertEqual({path.name: path.read_bytes() for path in (directory / "report").iterdir()}, previous)
        self.assertTrue(json.loads((directory / "commit.json").read_text())["sealed"])
