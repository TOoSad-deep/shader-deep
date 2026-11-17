"""公开 Python/CLI 入口交付多元素观察包并保留真实失败状态."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

from langchain_core.messages import AIMessage
from PIL import Image

from shader_deep.api import read_observation_package, read_report_package, run_analysis, run_analysis_task
from shader_deep.cli.analysis import main
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.scene_observations import SceneObservationSubmission
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.generation_inputs import capture_generation_inputs
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call

if TYPE_CHECKING:
    from shader_deep.workflows.analysis.session import AnalysisSession
    from shader_deep.workflows.outcomes import AnalysisOutcome


def report() -> dict[str, object]:
    return {
        "context_notes": ["白色背景"],
        "elements": [
            {"id": "E1", "name": "底层圆", "region": "中央偏左", "features": [{"id": "F2", "description": "蓝色圆形"}]},
            {"id": "E3", "name": "覆盖圆", "region": "中央偏右", "features": [{"id": "F9", "description": "浅灰圆形边缘"}]},
        ],
        "relations": [{"id": "R4", "participants": ["E1", "F9"], "description": "浅色圆覆盖蓝圆的右上部分"}],
    }


def submit(payload: dict[str, object], identity: str = "submit") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "save_observation_draft", "args": {"report": payload}, "id": identity}])


class PublicModel(NativeRoutingModel):
    pass


class ScenePublicApiTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.reference = self.root / "reference.png"
        Image.new("RGB", (8, 8), "pink").save(self.reference)
        self.options = AnalysisOptions(output_dir=self.root / "runs", max_request_retries=0)

    def run_model(self, *responses: AIMessage, options: AnalysisOptions | None = None) -> AnalysisOutcome:
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=PublicModel(responses=list(responses))):
            return run_analysis(self.reference, "分析整张图", options=options or self.options)

    def test_default_api_publishes_multielement_observations_without_planning(self) -> None:
        outcome = self.run_model(submit(report()))
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.summary_result.analysis_protocol, "scene_observations_v1")
        self.assertIsNone(outcome.summary_result.analysis_detail)
        manifest, observations = read_observation_package(outcome.report_dir)
        self.assertEqual(manifest.reference.sha256, hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertEqual([item.id for item in observations.elements], ["E1", "E3"])
        self.assertEqual(observations.elements[1].feature_ids, ("F9",))
        self.assertEqual(observations.relations[0].participants, ("E1", "F9"))
        self.assertFalse((outcome.report_dir / "mechanisms.json").exists())
        self.assertIn("E3 覆盖圆", (outcome.report_dir / "README.md").read_text())
        record = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(record["analysis_protocol"], "scene_observations_v1")
        self.assertEqual(record["options"]["max_model_calls"], 20)
        self.assertNotIn("max_tasks", record["options"])

    def test_task_entry_preserves_original_constraints(self) -> None:
        state = add_target(
            new_blackboard(), TargetRecord(version="T1", request="分析整张图", reference_path="reference.png", constraints=("保留柔影",))
        )
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="确认全图结构"))
        with patch("shader_deep.workflows.analysis.entry.build_model", return_value=PublicModel(responses=[submit(report())])):
            outcome = run_analysis_task(state, "A1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(state["results"], {})
        self.assertEqual(len(outcome.state["results"]), 1)

    def test_invalid_submission_can_be_repaired_in_same_graph(self) -> None:
        invalid = report()
        invalid["relations"] = [{"id": "R1", "participants": ["E1", "E8"], "description": "不合法引用"}]
        outcome = self.run_model(
            submit(invalid, "bad"), call("edit_file", {"file_path": "/draft/observations.json", "old_string": '"E8"', "new_string": '"E3"'})
        )
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(read_observation_package(outcome.report_dir)[1].relations[0].id, "R1")

    def test_declared_observation_gap_is_partial(self) -> None:
        payload = report()
        payload["gaps"] = [{"refs": ["F9"], "description": "局部过亮, 边缘细节尚未确认"}]
        outcome = self.run_model(submit(payload))
        self.assertEqual(outcome.stop_reason, "partial")
        self.assertEqual(read_observation_package(outcome.report_dir)[0].status, "partial")

    def test_text_and_stop_do_not_publish_empty_success(self) -> None:
        messages = [
            AIMessage(content="分析完成"),
            AIMessage(content="", tool_calls=[{"id": "stop", "name": "stop_analysis", "args": {"reason": "主体无法可靠分辨"}}]),
        ]
        for response in messages:
            with self.subTest(response=response.content):
                outcome = self.run_model(response)
                self.assertEqual(outcome.stop_reason, "failed")
                self.assertIsNone(outcome.report_dir)
                self.assertIsNone(outcome.summary_result)
                self.assertFalse((outcome.run_dir / "report").exists())

    def test_legacy_nondefault_options_fail_before_model_and_output(self) -> None:
        for changes in ({"max_tasks": 2}, {"max_worker_calls": 3}, {"worker_max_output_tokens": 100}):
            with self.subTest(changes=changes), patch("shader_deep.workflows.analysis.entry.build_model") as model, self.assertRaises(ValueError):
                run_analysis(self.reference, "分析", options=replace(self.options, **changes))
            model.assert_not_called()
        self.assertFalse((self.root / "runs").exists())

    def test_publish_failure_preserves_acceptance_but_records_failed(self) -> None:
        with patch("shader_deep.workflows.analysis.entry.write_observation_package", side_effect=OSError("publish disk failure")):
            outcome = self.run_model(submit(report()))
        self.assertEqual(outcome.stop_reason, "failed")
        self.assertIsNone(outcome.report_dir)
        self.assertIsNone(outcome.summary_result)
        self.assertTrue((outcome.run_dir / "results" / "observations.json").exists())
        for name in ("result.json", "run.json"):
            payload = json.loads((outcome.run_dir / name).read_text())
            self.assertIn("publish disk failure", payload["error"])

    def test_unwritable_final_index_propagates_original_io_error(self) -> None:
        with (
            patch("shader_deep.workflows.analysis.entry.save_run", side_effect=OSError("index disk failure")),
            self.assertRaisesRegex(OSError, "index disk failure"),
        ):
            self.run_model(submit(report()))

    def test_interrupt_after_acceptance_still_publishes_and_propagates(self) -> None:
        def interrupted_graph(model: object, session: AnalysisSession, options: object) -> SimpleNamespace:
            del model, options

            def invoke(*args: object, **kwargs: object) -> None:
                del args, kwargs
                session._submit(SceneObservationSubmission.model_validate(report()))
                raise KeyboardInterrupt

            return SimpleNamespace(invoke=invoke)

        with (
            patch("shader_deep.workflows.analysis.pipeline.build_analysis_agent", side_effect=interrupted_graph),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_model(submit(report()))
        directory = next((self.root / "runs").iterdir())
        self.assertEqual(read_observation_package(directory / "report")[0].status, "completed")
        self.assertEqual(json.loads((directory / "run.json").read_text())["stop_reason"], "completed")

    def test_observation_package_is_explicitly_rejected_by_planning_consumer(self) -> None:
        outcome = self.run_model(submit(report()))
        with self.assertRaisesRegex(ValueError, "M/S"):
            capture_generation_inputs(outcome.report_dir, self.root / "generation", "S1")
        self.assertFalse((self.root / "generation").exists())
        with self.assertRaises(ValueError):
            read_report_package(outcome.report_dir)

    def test_cli_outputs_short_index_and_finite_budget(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["shader-deep-analyze", str(self.reference), "分析整图", "--output-dir", str(self.root / "cli"), "--max-model-calls", "3"],
            ),
            patch("shader_deep.workflows.analysis.entry.build_model", return_value=PublicModel(responses=[submit(report())])),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["analysis_protocol"], "scene_observations_v1")
        self.assertTrue((Path(payload["report_dir"]) / "relations.json").exists())
        self.assertIn("README.md", stderr.getvalue())
