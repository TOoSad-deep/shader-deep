"""旧报告协议回归: 验证视觉初稿、前置取证及实际子任务材料的固定范围."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace

from pydantic import TypeAdapter

from shader_deep.compatibility.analysis.session import AnalysisSession
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.evidence import MeasurementRequest
from shader_deep.domain.legacy import AnalysisTaskRequest, VisualDecomposition
from shader_deep.domain.tasks import TargetRecord, TaskRecord
from shader_deep.workflows.options import AnalysisOptions
from tests.unit_tests.compatibility.test_analysis import context_payload, draft_batch, report_arguments, synthesis_arguments
from tests.unit_tests.compatibility.test_analysis_evidence import measurement, pixels
from tests.unit_tests.fixtures._analysis_fixture import AnalysisFixture, run_legacy_analysis
from tests.unit_tests.fixtures._generation_fixture import tool_results


def visual_draft(evidence_id: str, name: str = "左侧区域") -> dict[str, object]:
    return {
        "elements": [{"id": "E1", "name": name, "region": "left"}],
        "features": [
            {
                "id": "F1",
                "element_ids": ["E1"],
                "description": "左半区域较暗",
                "region": "left",
                "basis": "measurement_supported",
                "evidence_ids": [evidence_id],
            }
        ],
    }


class AnalysisVisualFlowTests(AnalysisFixture):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "reference.PNG").write_bytes(pixels())
        self.analysis_options = AnalysisOptions(output_dir=self.root / "analysis", max_main_calls=9, max_tasks=3, max_measurements=3)

    def session(self) -> AnalysisSession:
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="Inspect", reference_path=str(self.root / "reference.PNG")))
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="Inspect"))
        directory = self.root / "run-visual"
        directory.mkdir()
        return AnalysisSession(state, "A1", self.analysis_options, directory, "data:image/png;base64," + base64.b64encode(pixels()).decode())

    def test_preflight_evidence_and_revised_snapshot_reach_actual_worker_requests(self) -> None:
        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = context_payload(request)
            task = payload["task"]
            if task["lens_config"] is not None:
                self.assertEqual(payload["focus_feature_ids"], ["F1"])
                self.assertEqual(payload["visual_decomposition"]["features"][0]["evidence_ids"], [task["evidence_ids"][0]])
                self.assertEqual([item["id"] for item in payload["evidence"]], task["evidence_ids"])
                self.assertTrue(all(item["spec"]["region"]["left"] == 0 for item in payload["evidence"]))
                blocks = request["messages"][-1]["content"]
                images = [block["image_url"]["url"] for block in blocks if block.get("type") == "image_url"]
                self.assertEqual(base64.b64decode(images[0].split(",", 1)[1]), pixels())
                crop = next((item for item in payload["evidence"] if item["spec"]["kind"] == "crop"), None)
                self.assertEqual(len(images), 2 if crop else 1)
                if crop:
                    self.assertEqual(base64.b64decode(images[1].split(",", 1)[1]), self.root.joinpath(crop["artifact_path"]).read_bytes())
                if task["analysis_purpose"] == "initial":
                    self.assertEqual(payload["related_results"], [])
                    self.assertEqual(payload["visual_decomposition"]["elements"][0]["name"], "左侧区域")
                else:
                    self.assertEqual(payload["visual_decomposition"]["elements"][0]["name"], "修订后的左侧区域")
                arguments = report_arguments(payload)
                arguments["report"]["observations"][0].update(element_ids=["E1"], feature_ids=["F1"])
                return self.call("submit_analysis_report", arguments)
            if not payload["evidence"]:
                return self.call(
                    "measure_reference",
                    {
                        "requests": [
                            {"question": "Inspect", "measurement": spec}
                            for spec in (measurement(), measurement("crop"), measurement(left=4, right=8))
                        ]
                    },
                )
            selected = [item["id"] for item in payload["evidence"] if item["spec"]["region"]["left"] == 0]
            if not payload["related_results"]:
                requests = draft_batch()
                for index, item in enumerate(requests):
                    item.update(evidence_ids=selected if index == 0 else selected[:1], focus_feature_ids=["F1"])
                return self.call("run_analysis_batch", {"requests": requests, "visual_decomposition": visual_draft(selected[0])})
            if len(payload["related_results"]) == len(draft_batch()):
                if not any(any(item.get("status") == "cached" for item in receipt.get("results", [])) for receipt in tool_results(request)):
                    return self.call(
                        "measure_reference",
                        {
                            "requests": [
                                {"question": "Reuse", "measurement": measurement()},
                                {"question": "Over budget", "measurement": measurement("line_profile")},
                            ]
                        },
                    )
                return self.call(
                    "run_analysis_batch",
                    {
                        "requests": [
                            {
                                "preset_id": "graphics-2d",
                                "objective": "Check revised region",
                                "purpose": "verify",
                                "gap": "Boundary needs review",
                                "expected_evidence": "Visible boundary",
                                "evidence_ids": selected[:1],
                                "focus_feature_ids": ["F1"],
                            }
                        ],
                        "visual_decomposition": visual_draft(selected[0], "修订后的左侧区域"),
                    },
                )
            return self.call("finish_analysis", synthesis_arguments(payload))

        self.response = respond
        outcome = run_legacy_analysis(self.root / "reference.PNG", "Inspect", options=self.analysis_options)
        manifest = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(outcome.stop_reason, "completed", manifest)
        inputs = list(manifest["task_inputs"].values())
        self.assertEqual([item["visual_decomposition"]["elements"][0]["name"] for item in inputs], ["左侧区域", "左侧区域", "修订后的左侧区域"])
        self.assertEqual(len(outcome.state["measurements"]), 3)
        self.assertTrue(manifest["measurement_calls"][-1]["cached"])
        self.assertTrue(any("budget exhausted" in item["message"] for item in manifest["main_execution"]["tool_feedback"]))

    def test_unread_or_unselected_evidence_and_invalid_batch_preserve_preflight(self) -> None:
        session = self.session()
        request = TypeAdapter(MeasurementRequest).validate_python({"question": "Initial check", "measurement": measurement()})
        other = TypeAdapter(MeasurementRequest).validate_python({"question": "Other region", "measurement": measurement(left=4, right=8)})
        session._measure([request, other])
        evidence_ids = tuple(item.id for item in session.measurements.records.values())
        evidence_id = evidence_ids[0]
        visual = TypeAdapter(VisualDecomposition).validate_python(visual_draft(evidence_id))
        visual = replace(visual, features=(replace(visual.features[0], evidence_ids=evidence_ids),))
        requests = [replace(TypeAdapter(AnalysisTaskRequest).validate_python(item), evidence_ids=evidence_ids) for item in draft_batch()]
        with self.assertRaises(AnalysisValidationError) as invalid:
            session._register([replace(requests[0], evidence_ids=("unknown",)), requests[1]])
        self.assertEqual(invalid.exception.issues[0]["path"], "/requests/0/evidence_ids/0")
        self.assertNotIn("not_presented", str(invalid.exception))
        with self.assertRaises(AnalysisValidationError) as unread:
            session._register(requests, visual_decomposition=visual)
        self.assertEqual(
            {issue["path"] for issue in unread.exception.issues},
            {
                *(f"/requests/{index}/evidence_ids/{offset}" for index in range(2) for offset in range(2)),
                *(f"/visual_decomposition/features/0/evidence_ids/{offset}" for offset in range(2)),
            },
        )
        session.context()
        receipt = json.loads(
            session.tools()[0].invoke(
                {
                    "requests": [asdict(requests[0]), asdict(replace(requests[1], evidence_ids=()))],
                    "visual_decomposition": asdict(visual),
                }
            )
        )
        self.assertEqual(receipt["status"], "invalid_batch")
        self.assertIn("/requests/1/evidence_ids", receipt["message"])
        for identifier in evidence_ids:
            self.assertIn(identifier, receipt["message"])
        with self.assertRaisesRegex(ValueError, "Unknown preset"):
            session._register([requests[0], replace(requests[0], preset_id="missing")], visual_decomposition=visual)
        self.assertEqual(tuple(session.state["tasks"]), ("A1",))
        self.assertEqual(session.task_visual_inputs, {})
        self.assertEqual(session.workers, {})
        self.assertIsNone(session.visual_decomposition)
        self.assertIn(evidence_id, session.state["measurements"])
        tasks = session._register(requests, visual_decomposition=visual)
        self.assertEqual(len(tasks), 2)

    def test_legacy_batch_without_visual_structure_preserves_missing_semantics(self) -> None:
        session = self.session()
        requests = [TypeAdapter(AnalysisTaskRequest).validate_python(item) for item in draft_batch()]
        tasks = session._register(requests)
        self.assertTrue(all(session.task_visual_inputs[task.id] is None for task in tasks))
        payload = json.loads(session.context().content[0]["text"])
        self.assertIsNone(payload["visual_decomposition"])
