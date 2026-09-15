"""验证测量计算、协调器的执行权限及证据可见范围."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image
from pydantic import TypeAdapter, ValidationError

from shader_deep.agents.analysis import run_analysis
from shader_deep.analysis.evidence import ImageRegion, MeasurementRequest, MeasurementSpec
from shader_deep.analysis.measurements import ReferenceMeasurements
from shader_deep.analysis.profiles import summarize_profile
from shader_deep.analysis.schemas import LensReport
from shader_deep.analysis.types import AnalysisOptions, AnalysisOutcome
from shader_deep.blackboard import add_result, add_target, add_task, new_blackboard
from shader_deep.context.analysis import build_analysis_context
from shader_deep.schemas import ResultRecord, TargetRecord, TaskRecord
from tests.unit_tests._generation_fixture import GenerationFixture, tool_results
from tests.unit_tests.test_analysis import context_payload, draft_batch, report_arguments, synthesis_arguments


def pixels() -> bytes:
    with Image.new("RGB", (8, 6), (220, 220, 220)) as picture:
        picture.paste((20, 20, 20), (0, 0, 4, 6))
        stream = io.BytesIO()
        picture.save(stream, format="PNG")
        return stream.getvalue()


def measurement(kind: str = "region_stats", *, left: int = 0, right: int = 4, axis: str = "x") -> dict[str, object]:
    return {"kind": kind, "region": {"left": left, "top": 0, "right": right, "bottom": 6}, "axis": axis}


class MeasurementTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reader = ReferenceMeasurements(pixels(), self.root, "A1", "T1", 3)

    def request(self, spec: dict[str, object], question: str = "Compare brightness") -> MeasurementRequest:
        return TypeAdapter(MeasurementRequest).validate_python({"question": question, "measurement": spec})

    def test_region_statistics_and_profiles_use_original_pixels(self) -> None:
        dark, _ = self.reader.measure(self.request(measurement()))
        light, _ = self.reader.measure(self.request(measurement(left=4, right=8)))
        profile, _ = self.reader.measure(self.request(measurement("line_profile", right=8)))
        self.assertEqual(dark.mean_rgb, (20.0, 20.0, 20.0))
        self.assertEqual(light.mean_luma, 220.0)
        self.assertEqual(profile.profile, (20.0,) * 4 + (220.0,) * 4)
        self.assertEqual(profile.image_size, (8, 6))

    def test_deduplication_ignores_question_and_irrelevant_axis_but_keeps_geometry(self) -> None:
        self.reader.limit = 1
        first, cached = self.reader.measure(self.request(measurement()))
        reused, reused_flag = self.reader.measure(self.request(measurement(axis="y"), "Another worker asks the same thing"))
        self.assertFalse(cached)
        self.assertTrue(reused_flag)
        self.assertIs(first, reused)
        self.assertEqual(len(self.reader.records), 1)
        self.assertEqual(len(self.reader.calls), 2)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.reader.measure(self.request(measurement(left=4, right=8)))
        self.assertEqual(len(self.reader.records), 1)

    def test_crop_is_exact_and_profile_axis_changes_the_operation(self) -> None:
        crop, _ = self.reader.measure(self.request(measurement("crop")))
        with Image.open(crop.artifact_path) as picture:
            self.assertEqual(picture.size, (4, 6))
            self.assertEqual(picture.getpixel((2, 3)), (20, 20, 20, 255))
        profile, _ = self.reader.measure(self.request(measurement("line_profile", right=8, axis="y")))
        self.assertEqual(profile.profile, (120.0,) * 6)
        self.assertIsNone(crop.mean_luma)

    def test_invalid_coordinates_and_specs_do_not_create_artifacts(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            self.reader.measure(self.request(measurement(right=9)))
        for region in ({"left": 4, "top": 0, "right": 2, "bottom": 6}, {"left": True, "top": 0, "right": 4, "bottom": 6}):
            with self.assertRaises(ValidationError):
                TypeAdapter(ImageRegion).validate_python(region)
        with self.assertRaises(ValidationError):
            TypeAdapter(MeasurementSpec).validate_python({**measurement(), "path": "/arbitrary.png"})
        self.assertEqual(self.reader.records, {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_alpha_statistics_explicitly_composite_on_white(self) -> None:
        stream = io.BytesIO()
        with Image.new("RGBA", (1, 1), (0, 0, 0, 0)) as image:
            image.save(stream, format="PNG")
        reader = ReferenceMeasurements(stream.getvalue(), self.root, "A1", "T1", 1)
        spec = MeasurementSpec(kind="region_stats", region=ImageRegion(left=0, top=0, right=1, bottom=1))
        result, _ = reader.measure(MeasurementRequest(question="Transparent background", measurement=spec))
        self.assertEqual(result.mean_luma, 255.0)
        self.assertEqual(result.alpha_policy, "composite_on_white_for_statistics")

    def test_profile_digest_locates_dark_lines_in_original_coordinates(self) -> None:
        stream = io.BytesIO()
        with Image.new("RGB", (500, 8), "white") as image:
            image.paste("black", (80, 0, 81, 8))
            image.paste("black", (260, 0, 261, 8))
            image.paste("black", (440, 0, 441, 8))
            image.save(stream, format="PNG")
        reader = ReferenceMeasurements(stream.getvalue(), self.root, "A1", "T1", 1)
        spec = MeasurementSpec(kind="line_profile", region=ImageRegion(left=40, top=0, right=480, bottom=8))
        result, _ = reader.measure(MeasurementRequest(question="Locate grid lines", measurement=spec))
        self.assertEqual([point.position for point in result.profile_digest.dark_extrema], [80, 260, 440])
        self.assertEqual(result.profile_digest.count, 440)
        self.assertFalse(result.profile_digest.extrema_truncated)
        self.assertEqual(len(result.profile), 440)

    def test_single_pixel_profile_and_empty_profile(self) -> None:
        digest = summarize_profile((12.0,), 37)
        self.assertEqual([(point.position, point.value) for point in digest.samples], [(37, 12.0)])
        self.assertEqual(digest.dark_extrema, ())
        with self.assertRaisesRegex(ValueError, "at least one"):
            summarize_profile((), 0)

    def test_dark_line_does_not_create_bright_shoulders_on_flat_or_sloping_background(self) -> None:
        for slope in (0.0, 0.1):
            values = [220 - index * slope for index in range(100)]
            values[50] -= 20
            digest = summarize_profile(tuple(values), 200)
            self.assertEqual([point.position for point in digest.dark_extrema], [250])
            self.assertEqual(digest.bright_extrema, ())

    def test_bright_line_does_not_create_false_dark_shoulders(self) -> None:
        values = [80.0] * 100
        values[50] += 20
        digest = summarize_profile(tuple(values), 0)
        self.assertEqual([point.position for point in digest.bright_extrema], [50])
        self.assertEqual(digest.dark_extrema, ())
        self.assertEqual(digest.bright_extrema[0].neighbor_level, 80.0)

    def test_small_noise_near_dark_line_is_not_a_strong_bright_peak(self) -> None:
        values = [220.0] * 100
        values[50] = 200.0
        values[47] += 0.2
        values[53] += 0.2
        digest = summarize_profile(tuple(values), 0)
        self.assertEqual([point.position for point in digest.dark_extrema], [50])
        self.assertEqual(digest.bright_extrema, ())

    def test_context_shows_positioned_digest_and_preserves_full_record(self) -> None:
        with Image.new("RGB", (500, 8), "white") as picture:
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
        reader = ReferenceMeasurements(stream.getvalue(), self.root, "A1", "T1", 1)
        spec = MeasurementSpec(kind="line_profile", region=ImageRegion(left=0, top=0, right=500, bottom=8))
        result, _ = reader.measure(MeasurementRequest(question="Overview", measurement=spec))
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="Inspect", reference_path="reference.png"))
        state = add_task(state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="Inspect"))
        state = {**state, "measurements": {result.id: result}}
        message = build_analysis_context(state, "A1", "data:image/png;base64,fixture")
        evidence = json.loads(message.content[0]["text"])["evidence"][0]
        self.assertNotIn("profile", evidence)
        self.assertEqual(evidence["profile_digest"]["count"], 500)
        self.assertEqual(evidence["profile_digest"]["samples"][-1]["position"], 499)
        self.assertEqual(len(state["measurements"][result.id].profile), 500)


class AnalysisEvidenceTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "reference.PNG").write_bytes(pixels())
        self.options = AnalysisOptions(output_dir=self.root / "analysis", max_main_calls=6, max_tasks=3)
        self.response = self.respond_to_analysis

    def run_case(self) -> AnalysisOutcome:
        return run_analysis(self.root / "reference.PNG", "Analyze this image", options=self.options)

    def respond_to_analysis(self, request: dict[str, object]) -> dict[str, object]:
        payload = context_payload(request)
        task = payload["task"]
        if task["lens_config"] is not None:
            arguments = report_arguments(payload)
            if task["analysis_purpose"] == "initial":
                self.assertEqual(payload["evidence"], [])
                arguments["report"]["evidence_requests"] = [{"question": "Is the left region darker?", "why_it_matters": "Preserve contrast"}]
            else:
                self.assertEqual(len(payload["evidence"]), 2)
                self.assertEqual({item["spec"]["kind"] for item in payload["evidence"]}, {"region_stats", "crop"})
                numeric = next(item for item in payload["evidence"] if item["spec"]["kind"] == "region_stats")
                self.assertEqual(numeric["mean_luma"], 20.0)
                arguments["report"]["observations"][0].update(basis="measurement_supported", evidence_ids=[numeric["id"]])
                blocks = request["messages"][-1]["content"]
                self.assertEqual(sum(block.get("type") == "image_url" for block in blocks), 2)
            self.assertEqual({item["function"]["name"] for item in request["tools"]}, {"submit_analysis_report"})
            return self.call("submit_analysis_report", arguments)
        if not payload["related_results"]:
            return self.call("run_analysis_batch", {"requests": draft_batch()})
        if not payload["evidence"]:
            return self.call(
                "measure_reference",
                {
                    "requests": [
                        {"question": "Left brightness", "measurement": measurement()},
                        {"question": "Duplicate demand", "measurement": measurement()},
                        {"question": "Inspect left", "measurement": measurement("crop")},
                        {"question": "Right brightness", "measurement": measurement(left=4, right=8)},
                    ]
                },
            )
        if len(payload["related_results"]) == len(draft_batch()):
            selected = [item["id"] for item in payload["evidence"] if item["spec"]["region"]["left"] == 0]
            return self.call(
                "run_analysis_batch",
                {
                    "requests": [
                        {
                            "preset_id": "graphics-2d",
                            "objective": "Verify brightness",
                            "purpose": "verify",
                            "gap": "Brightness determines contrast",
                            "expected_evidence": "Regional statistics and a local view",
                            "related_result_ids": [payload["related_results"][0]["id"]],
                            "evidence_ids": selected,
                        }
                    ]
                },
            )
        arguments = synthesis_arguments(payload)
        arguments["summary"]["key_observations"][0].update(
            basis="measurement_supported",
            evidence_ids=[payload["evidence"][0]["id"]],
        )
        return self.call("finish_analysis", arguments)

    def test_batch_measurement_deduplication_selected_delivery_and_saved_sources(self) -> None:
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed", outcome.summary_result)
        saved = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(len(saved["blackboard"]["measurements"]), 3)
        self.assertEqual([call["cached"] for call in saved["measurement_calls"]], [False, True, False, False])
        self.assertEqual(len(outcome.summary_result.analysis_detail.source_result_ids), 3)
        self.assertEqual(outcome.summary_result.analysis_detail.key_observations[0].basis, "measurement_supported")
        self.assertEqual(len([task for task in outcome.state["tasks"].values() if task.analysis_purpose == "verify"]), 1)

    def test_measurement_before_initial_reports_is_rejected(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and not attempted:
                attempted = True
                return self.call("measure_reference", {"requests": [{"question": "Too early", "measurement": measurement()}]})
            return self.respond_to_analysis(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertTrue(any("before measuring" in str(tool_results(request)) for request in self.requests))

    def test_same_turn_cannot_cite_unseen_measurements(self) -> None:
        attempted = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal attempted
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"] and not payload["evidence"] and not attempted:
                attempted = True
                message = self.respond_to_analysis(request)
                arguments = synthesis_arguments(payload)
                name = payload["related_results"][0]["id"].rsplit("-a", 1)[0]
                arguments["summary"]["key_observations"][0].update(basis="measurement_supported", evidence_ids=[f"{name}-e001"])
                arguments["text"] = "Unseen measurements"
                finish = self.call("finish_analysis", arguments)
                finish["tool_calls"][0]["id"] += "-blind"
                message["tool_calls"].extend(finish["tool_calls"])
                return message
            return self.respond_to_analysis(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertNotEqual(outcome.summary_result.summary, "Unseen measurements")
        self.assertTrue(any("Receive measurement results" in str(tool_results(request)) for request in self.requests))

    def test_worker_cannot_execute_measurements(self) -> None:
        attempted = set()

        def response(request: dict[str, object]) -> dict[str, object]:
            task = context_payload(request)["task"]
            if task["lens_config"] is not None and task["id"] not in attempted:
                attempted.add(task["id"])
                return self.call("measure_reference", {"requests": [{"question": "Worker attempt", "measurement": measurement()}]})
            return self.respond_to_analysis(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        saved = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertFalse(any(call["question"] == "Worker attempt" for call in saved["measurement_calls"]))
        self.assertTrue(any("未开放此工具" in str(tool_results(request)) for request in self.requests))

    def test_measurements_use_frozen_bytes_when_original_path_changes(self) -> None:
        changed = False

        def response(request: dict[str, object]) -> dict[str, object]:
            nonlocal changed
            payload = context_payload(request)
            if payload["task"]["lens_config"] is None and payload["related_results"] and not changed:
                (self.root / "reference.PNG").write_bytes(b"not the reference anymore")
                changed = True
            return self.respond_to_analysis(request)

        self.response = response
        outcome = self.run_case()
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual((outcome.run_dir / "reference.png").read_bytes(), pixels())

    def test_foreign_and_unprovided_evidence_cannot_be_cited(self) -> None:
        outcome = self.run_case()
        state = outcome.state
        record = next(iter(state["measurements"].values()))
        child = next(task for task in state["tasks"].values() if task.analysis_purpose == "verify")
        root = replace(state["tasks"]["A1"], id="A2")
        state = add_task(state, root)
        with self.assertRaisesRegex(ValueError, "outside analysis"):
            add_task(state, replace(child, id="foreign", parent_task_id="A2", related_result_ids=(), evidence_ids=(record.id,)))
        unprovided = replace(child, id="no-evidence", evidence_ids=())
        state = add_task(state, unprovided)
        report = TypeAdapter(LensReport).validate_python(
            {
                "scope": "Local",
                "observations": [
                    {
                        "id": "O1",
                        "text": "Measured",
                        "region": "left",
                        "basis": "measurement_supported",
                        "evidence_ids": [record.id],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "not provided"):
            add_result(state, ResultRecord(id="bad-report", task_id=unprovided.id, status="completed", summary="Bad", analysis_detail=report))

    def test_visual_crop_alone_cannot_claim_numeric_measurement_support(self) -> None:
        outcome = self.run_case()
        child = next(task for task in outcome.state["tasks"].values() if task.analysis_purpose == "verify")
        crop = next(item for item in outcome.state["measurements"].values() if item.spec.kind == "crop")
        report = TypeAdapter(LensReport).validate_python(
            {
                "scope": "Local",
                "observations": [
                    {
                        "id": "O1",
                        "text": "Measured",
                        "region": "left",
                        "basis": "measurement_supported",
                        "evidence_ids": [crop.id],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "numeric measurement"):
            add_result(outcome.state, ResultRecord(id="bad", task_id=child.id, status="completed", summary="Bad", analysis_detail=report))
