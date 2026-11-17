"""观察运行的诊断 IO 与用户中断不影响合法结果核查."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from PIL import Image

from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.analysis.pipeline import execute_scene_analysis
from shader_deep.workflows.analysis.session import AnalysisSession
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call, observation

if TYPE_CHECKING:
    from shader_deep.domain.scene_observations import SceneObservationSubmission
    from shader_deep.workflows.analysis.contracts import SceneAnalysisResult


class NamedUsageModel(NativeRoutingModel):
    """提供安全模型标识, 与可能带有凭据的连接配置分离."""

    model_name: str = "offline-model"


class ScenePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buffer, format="PNG")
        self.reference = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

    def test_available_usage_rule_identity_and_repair_counts_are_recorded(self) -> None:
        denied = call("task", {"subagent_type": "general-purpose", "description": "delegate"})
        invalid = call("save_observation_draft", {"report": {"elements": "invalid"}}, 2)
        accepted = call("save_observation_draft", {"report": observation()}, 3)
        responses = [
            message.model_copy(
                update={
                    "usage_metadata": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                    "response_metadata": {"model_name": "offline-model"},
                }
            )
            for message in (denied, invalid, accepted)
        ]
        result = execute_scene_analysis(
            "观察", self.reference, AnalysisOptions(max_request_retries=0), self.directory, model=NamedUsageModel(responses=responses)
        )
        metrics = result.task_state
        self.assertEqual(metrics["token_usage"], {"input_tokens": 30, "output_tokens": 6, "total_tokens": 36})
        self.assertEqual(metrics["model_id"], "offline-model")
        self.assertEqual((metrics["model_calls"], metrics["tool_refusals"], metrics["validation_errors"]), (5, 1, 1))
        resource = files("shader_deep.resources").joinpath("skills", "scene-observer", "SKILL.md")
        self.assertEqual(metrics["rule_sha256"], hashlib.sha256(resource.read_bytes()).hexdigest())
        resources = files("shader_deep.resources").joinpath("skills", "scene-observer")
        for name, digest in metrics["resource_sha256"].items():
            self.assertEqual(digest, hashlib.sha256(resources.joinpath(name).read_bytes()).hexdigest())
        expected = hashlib.sha256(json.dumps(metrics["resource_sha256"], sort_keys=True).encode()).hexdigest()
        self.assertEqual(metrics["skill_bundle_sha256"], expected)
        self.assertGreaterEqual(metrics["elapsed_seconds"], 0)
        self.assertIsNone(metrics["http_request_count"])

    def test_unreported_usage_is_unknown_instead_of_zero(self) -> None:
        result = execute_scene_analysis("观察", self.reference, AnalysisOptions(max_request_retries=0), self.directory, model=NativeRoutingModel())
        self.assertIsNone(result.task_state["token_usage"])
        self.assertIsNone(result.task_state["model_id"])

    def test_one_failed_diagnostic_does_not_prevent_other_diagnostics_or_delivery(self) -> None:
        attempts = []
        deliveries = []

        def write(path: Path, payload: object) -> None:
            attempts.append(path.name)
            if path.name == "manifest.json" and (self.directory / "results" / "observations.json").exists():
                msg = "manifest unavailable"
                raise OSError(msg)
            write_snapshot(path, payload)

        def deliver(result: SceneAnalysisResult, session: AnalysisSession) -> SceneAnalysisResult:
            del session
            deliveries.append(result)
            return result

        with patch("shader_deep.workflows.analysis.session.write_snapshot", side_effect=write):
            result = execute_scene_analysis(
                "观察", self.reference, AnalysisOptions(max_request_retries=0), self.directory, model=NativeRoutingModel(), on_delivery=deliver
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(deliveries), 1)
        self.assertIn("diagnostic_error", result.error)
        self.assertIn("result.json", attempts)
        self.assertTrue((self.directory / "result.json").is_file())
        self.assertTrue((self.directory / "results" / "observations.json").is_file())

    def test_all_diagnostics_fail_but_delivery_is_still_attempted_before_original_io(self) -> None:
        deliveries = []

        def write(path: Path, payload: object) -> None:
            if (self.directory / "results" / "observations.json").exists():
                msg = "original disk failure"
                raise OSError(msg)
            write_snapshot(path, payload)

        def deliver(result: SceneAnalysisResult, session: AnalysisSession) -> SceneAnalysisResult:
            del session
            deliveries.append(result)
            msg = "later public index failure"
            raise OSError(msg)

        with (
            patch("shader_deep.workflows.analysis.session.write_snapshot", side_effect=write),
            self.assertRaisesRegex(OSError, "original disk failure"),
        ):
            execute_scene_analysis(
                "观察", self.reference, AnalysisOptions(max_request_retries=0), self.directory, model=NativeRoutingModel(), on_delivery=deliver
            )
        self.assertEqual(len(deliveries), 1)
        self.assertTrue((self.directory / "results" / "observations.json").is_file())

    def test_interrupt_survives_a_failing_delivery_callback(self) -> None:
        original = AnalysisSession._submit
        deliveries = []

        def interrupt(session: AnalysisSession, report: SceneObservationSubmission) -> None:
            original(session, report)
            raise KeyboardInterrupt

        def deliver(result: SceneAnalysisResult, session: AnalysisSession) -> SceneAnalysisResult:
            del session
            deliveries.append(result)
            msg = "callback disk failure"
            raise OSError(msg)

        with patch.object(AnalysisSession, "_submit", interrupt), self.assertRaises(KeyboardInterrupt):
            execute_scene_analysis(
                "观察", self.reference, AnalysisOptions(max_request_retries=0), self.directory, model=NativeRoutingModel(), on_delivery=deliver
            )
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].status, "completed")
        self.assertTrue((self.directory / "results" / "observations.json").is_file())
