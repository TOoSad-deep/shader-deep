"""旧报告协议回归: 保护渐进披露的真实请求、条目回读与固定输入边界."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace
from unittest.mock import patch

from langchain.messages import ToolMessage
from pydantic import TypeAdapter

from shader_deep.analysis.evidence import MeasurementRequest
from shader_deep.analysis.report_files import AnalysisReportFiles
from shader_deep.analysis.schemas import (
    AnalysisSummary,
    AnalysisTaskRequest,
    AnalysisValidationError,
    LensReport,
    SourceRef,
    VisualDecomposition,
    VisualElement,
    VisualMapping,
)
from shader_deep.analysis.session import AnalysisSession
from shader_deep.analysis.types import AnalysisOptions
from shader_deep.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.schemas import ResultRecord, TargetRecord, TaskRecord
from tests.unit_tests._analysis_fixture import raw_context_payload, run_legacy_analysis, run_legacy_analysis_task
from tests.unit_tests._generation_fixture import GenerationFixture, tool_results
from tests.unit_tests.test_analysis import draft_batch, report_arguments
from tests.unit_tests.test_analysis_evidence import measurement, pixels


class AnalysisDisclosureTests(GenerationFixture):
    def test_root_history_is_presented_without_expanding_current_report_scope(self) -> None:
        historical: list[dict[str, object]] = []

        def respond(request: dict[str, object]) -> dict[str, object]:
            payload = raw_context_payload(request)
            if payload["task"]["lens_config"] is not None:
                self.assertEqual(payload["related_results"], [])
                self.assertEqual(payload["source_catalog"], [])
                return self.call("submit_analysis_report", report_arguments(payload))
            self.assertEqual(payload["related_results"], historical)
            files = payload["report_files"]
            historical_ids = {item["id"] for item in historical}
            self.assertFalse(historical_ids & {item["result_id"] for item in files})
            self.assertFalse(historical_ids & {item["result_id"] for item in payload["source_catalog"]})
            if not files:
                return self.call("run_analysis_batch", {"requests": draft_batch()})
            if not any(item.get("status") == "read" for item in tool_results(request)):
                return self.call("read_analysis_file", {"file_path": files[0]["file_path"], "pointer": "/analysis_detail/observations/0"})
            return self.call(
                "finish_analysis",
                {
                    "text": "Current inspection",
                    "summary": {
                        "source_result_ids": [item["result_id"] for item in files],
                        "key_observations": [{"text": "Visible structure", "source_refs": [{"result_id": files[0]["result_id"], "item_id": "O1"}]}],
                    },
                },
            )

        self.response = respond
        options = AnalysisOptions(output_dir=self.root / "analysis", max_main_calls=4, max_worker_calls=2)
        first = run_legacy_analysis(self.root / "reference.PNG", "Inspect", options=options)
        self.assertEqual(first.stop_reason, "completed")
        selected = (first.summary_result.analysis_detail.source_result_ids[0], first.summary_result.id)
        historical.extend(json.loads(json.dumps(asdict(first.state["results"][identifier]))) for identifier in selected)
        state = add_task(
            first.state, TaskRecord(id="next-root", role="analysis", target_version="T1", objective="Reinspect", related_result_ids=selected)
        )
        second = run_legacy_analysis_task(state, "next-root", options=options)
        self.assertEqual(second.stop_reason, "completed", json.loads((second.run_dir / "run.json").read_text()))
        self.assertFalse(set(first.state["results"]) & set(second.summary_result.analysis_detail.source_result_ids))

    def test_actual_request_reads_only_selected_body_and_blocks_same_batch_finish(self) -> None:
        attempted = False

        def respond(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = raw_context_payload(request)
            names = {item["function"]["name"] for item in request["tools"]}
            if payload["task"]["lens_config"] is not None:
                self.assertNotIn("read_analysis_file", names)
                self.assertEqual(payload["related_results"], [])
                return self.call("submit_analysis_report", report_arguments(payload))
            self.assertIn("read_analysis_file", names)
            self.assertEqual(payload["related_results"], [])
            self.assertIn("data:image/png;base64,", str(request["messages"]))
            files = payload["report_files"]
            if not files:
                return self.call("run_analysis_batch", {"requests": draft_batch()})
            self.assertNotIn("Other explanations remain possible", json.dumps(payload))
            identifier = files[0]["result_id"]
            finish = self.call(
                "finish_analysis",
                {
                    "text": "One inspected observation, alternative explanations remain in source reports",
                    "summary": {
                        "source_result_ids": [item["result_id"] for item in files],
                        "key_observations": [{"text": "Visible structure", "source_refs": [{"result_id": identifier, "item_id": "O1"}]}],
                    },
                },
            )
            if not attempted:
                attempted = True
                read = self.call("read_analysis_file", {"file_path": files[0]["file_path"], "pointer": "/analysis_detail/observations/0"})
                finish["tool_calls"][0]["id"] += "-finish"
                read["tool_calls"].extend(finish["tool_calls"])
                return read
            self.assertTrue(any("source_not_presented" in json.dumps(receipt) for receipt in tool_results(request)))
            self.assertTrue(any(receipt.get("status") == "read" and receipt["content"]["id"] == "O1" for receipt in tool_results(request)))
            return finish

        self.response = respond
        outcome = run_legacy_analysis(
            self.root / "reference.PNG", "Inspect", options=AnalysisOptions(output_dir=self.root / "analysis", max_main_calls=4)
        )
        manifest = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(outcome.stop_reason, "completed", manifest)
        self.assertEqual(manifest["main_execution"]["model_calls"], 3)
        self.assertEqual(len(list((outcome.run_dir / "reports").glob("*.json"))), 2)
        self.assertEqual(sum(bool(items) for items in manifest["presented_report_pointers"].values()), 1)
        self.assertEqual(len(manifest["unlinked_interpretations"]), 2)

    def test_complete_sections_accumulate_but_truncated_or_oversized_reads_do_not(self) -> None:
        store = AnalysisReportFiles(self.root)
        report = TypeAdapter(LensReport).validate_python(report_arguments({"task": {"lens_config": {"name": "test"}}})["report"])
        result = ResultRecord(id="R1", task_id="A2", status="completed", summary="short summary", analysis_detail=report)
        store.register(result)
        for pointer in ("/id", "/analysis_detail/kind", "/analysis_detail/observations/0/id"):
            body = store.read("/R1.json", pointer, pointer)
            store.on_prepared([ToolMessage(content=body, tool_call_id=pointer)])
        self.assertEqual(store.body_count(), 0)
        observation = SourceRef(result_id="R1", item_id="O1")
        body = store.read("/R1.json", "/analysis_detail/observations/0", "read-O1")
        self.assertFalse(store.has_source(observation))
        store.on_prepared([ToolMessage(content="Tool output saved to a file", tool_call_id="read-O1")])
        self.assertFalse(store.has_source(observation))
        store.on_prepared([ToolMessage(content=body, tool_call_id="read-O1")])
        self.assertTrue(store.has_source(observation))
        self.assertEqual(store.body_count(), 1)
        self.assertFalse(store.has_source(SourceRef(result_id="R1", item_id="H1")))
        self.assertFalse(store.has_source(SourceRef(result_id="R1")))
        with patch("shader_deep.analysis.report_files.MAX_REPORT_RESPONSE_CHARS", 200):
            response = json.loads(store.read("/R1.json", "", "oversized"))
        self.assertEqual(response["status"], "too_large")
        self.assertFalse(response["complete"])
        with self.assertRaisesRegex(ValueError, "Choose a file_path"):
            store.read("/../run.json", "", "outside")
        pointers = [f"/{key}" for key in asdict(result) if key != "analysis_detail"]
        pointers.extend(f"/analysis_detail/{key}" for key in asdict(report) if key != "observations")
        for pointer in pointers:
            body = store.read("/R1.json", pointer, pointer)
            store.on_prepared([ToolMessage(content=body, tool_call_id=pointer)])
        self.assertTrue(store.has_source(SourceRef(result_id="R1")))
        completed = store.body_count()
        body = store.read("/R1.json", "/analysis_detail/interpretations/0", "overlapping")
        self.assertEqual(store.on_prepared([ToolMessage(content=body, tool_call_id="overlapping")]), 0)
        body = store.read("/R1.json", "", "parent")
        store.on_prepared([ToolMessage(content=body, tool_call_id="parent")])
        self.assertEqual(store.body_count(), completed)

    def test_measurement_phase_cache_and_visual_snapshot_deduplication(self) -> None:
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="Inspect", reference_path="reference.png"))
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="Inspect"))
        session = AnalysisSession(
            state, "A1", AnalysisOptions(max_measurements=3), self.root, "data:image/png;base64," + base64.b64encode(pixels()).decode()
        )
        before = json.loads(session.context().content[0]["text"])
        self.assertEqual(before["limits"]["phase"], "initial_observation")
        request = TypeAdapter(MeasurementRequest).validate_python({"question": "Inspect", "measurement": measurement()})
        first = json.loads(session._measure([request]))
        cached = json.loads(session._measure([request]))
        self.assertEqual(first["measurements_remaining"], cached["measurements_remaining"])
        self.assertEqual(cached["results"][0]["status"], "cached")
        original = VisualDecomposition(elements=(VisualElement(id="E1", name="initial shape", region="whole"),))
        requests = [TypeAdapter(AnalysisTaskRequest).validate_python(value) for value in draft_batch()]
        session._register(requests, visual_decomposition=original)
        session.visual_decomposition = replace(original, elements=(replace(original.elements[0], name="revised shape"),))
        payload = json.loads(session.context().content[0]["text"])
        self.assertEqual(payload["limits"]["phase"], "review")
        self.assertEqual(payload["limits"]["measurements_used"], 1)
        self.assertEqual(len(payload["visual_snapshots"]), 1)
        self.assertEqual(set(payload["task_visual_snapshot_ids"].values()), {payload["initial_visual_snapshot_id"]})

    def test_mapping_uses_presented_task_snapshot_but_requires_reading_new_objects(self) -> None:
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="Inspect", reference_path="reference.png"))
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="Inspect"))
        session = AnalysisSession(state, "A1", AnalysisOptions(), self.root, "data:image/png;base64," + base64.b64encode(pixels()).decode())
        visual = VisualDecomposition(elements=(VisualElement(id="E1", name="initial shape", region="whole"),))
        tasks = session._register([TypeAdapter(AnalysisTaskRequest).validate_python(value) for value in draft_batch()], visual_decomposition=visual)
        report = TypeAdapter(LensReport).validate_python(report_arguments({"task": {"lens_config": {"name": "test"}}})["report"])
        report = replace(report, visual_additions=VisualDecomposition(elements=(VisualElement(id="E2", name="new shape", region="center"),)))
        for task in tasks:
            session.state = add_result(
                session.state, ResultRecord(id=task.id + "-report", task_id=task.id, status="completed", summary="Report", analysis_detail=report)
            )
            session.workers[task.id].status = "completed"
        context = session.context()
        self.assertEqual(session.presented_visual_inputs, set())
        session.on_prepared([context])
        identifier = tasks[0].id + "-report"
        request = TypeAdapter(MeasurementRequest).validate_python({"question": "Inspect", "measurement": measurement()})
        session._measure([request])
        evidence_id = next(iter(session.measurements.records.values())).id
        followup = replace(
            TypeAdapter(AnalysisTaskRequest).validate_python(draft_batch()[0]),
            purpose="verify",
            gap="Boundary remains unclear",
            expected_evidence="A second inspection",
            related_result_ids=("unknown-report",),
            evidence_ids=(evidence_id,),
        )
        with self.assertRaises(AnalysisValidationError) as invalid_batch:
            session._register([followup])
        self.assertEqual(invalid_batch.exception.issues[0]["path"], "/requests/0/related_result_ids/0")
        self.assertNotIn("not_presented", str(invalid_batch.exception))
        with self.assertRaises(AnalysisValidationError) as unread_batch:
            session._register([replace(followup, related_result_ids=(identifier,))])
        self.assertEqual(
            {issue["path"] for issue in unread_batch.exception.issues},
            {"/requests/0/evidence_ids/0", "/requests/0/related_result_ids/0"},
        )
        self.assertEqual(len(session.state["tasks"]), len(tasks) + 1)
        summary = TypeAdapter(AnalysisSummary).validate_python(
            {
                "source_result_ids": [task.id + "-report" for task in tasks],
                "key_observations": [
                    {"text": "Visible shape", "source_refs": [{"result_id": identifier, "item_id": "O1"}], "evidence_ids": [evidence_id]}
                ],
                "visual_decomposition": asdict(visual),
            }
        )
        snapshot_mapping = VisualMapping(source_result_id=identifier, source_id="E1", target_id="E1", kind="element")
        new_mapping = replace(snapshot_mapping, source_id="E2")
        invalid = replace(summary.key_observations[0], evidence_ids=("unknown-measurement",))
        with self.assertRaises(AnalysisValidationError) as rejected:
            session._finish(replace(summary, key_observations=(invalid,)), "Inspection")
        self.assertIn("outside analysis", str(rejected.exception))
        self.assertNotIn("not_presented", str(rejected.exception))
        with self.assertRaises(AnalysisValidationError) as unread:
            session._finish(replace(summary, visual_mappings=(snapshot_mapping, new_mapping)), "Inspection")
        self.assertEqual(
            {issue["path"] for issue in unread.exception.issues},
            {
                "/summary/key_observations/0/source_refs/0",
                "/summary/key_observations/0/evidence_ids/0",
                "/summary/visual_mappings/1/source_id",
            },
        )
        self.assertIsNone(session.summary_result)
        self.assertNotIn(f"{self.root.name}-summary", session.state["results"])
        session.context()
        body = session.report_files.read(f"/{identifier}.json", "/analysis_detail/observations/0", "observation")
        session.on_prepared([ToolMessage(content=body, tool_call_id="observation")])
        receipt = json.loads(session._finish(replace(summary, visual_mappings=(snapshot_mapping,)), "Inspection"))
        self.assertEqual(receipt["status"], "completed")
