"""保护冻结坐标、数值诊断和独立视觉回执的失效边界."""

import hashlib
import json
import math
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.experiments.parameter_evaluation import ReferenceSpec, build_judge_request, evaluate_render, freeze_reference, parse_judge_result
from shader_deep.experiments.parameter_evaluation._judge import prepare_judge_request
from shader_deep.experiments.parameter_evaluation._metrics import background_metrics, decode_png, region_metrics

MODULE = "shader_deep.experiments.parameter_evaluation"
PAIR = "a" * 64


def png_bytes(image: Image.Image) -> bytes:
    with BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def valid_verdict() -> dict[str, object]:
    return {
        "pair_id": PAIR,
        "overall": "pass",
        "criteria": {
            name: {"status": "pass", "evidence": [{"rectangle": [330, 145, 695, 435], "observation": "两个圆的边界与参考一致"}]}
            for name in ("geometry_layout", "overlap_transparency", "color_placement", "edges_transitions")
        },
    }


class ParameterMetricTests(TestCase):
    def test_identical_pixels_have_zero_errors_and_unit_ssim(self) -> None:
        with Image.new("RGB", (11, 10), (12, 93, 170)) as image:
            result = region_metrics(image, image, (1, 1, 10, 9))
        self.assertEqual(result["pixels"], 72)
        self.assertEqual(result["mae"], 0)
        self.assertEqual(result["rmse"], 0)
        self.assertAlmostEqual(result["ssim_luma"], 1, places=10)

    def test_black_white_has_analytic_error_and_ssim(self) -> None:
        with Image.new("RGB", (9, 9), "black") as black, Image.new("RGB", (9, 9), "white") as white:
            result = region_metrics(black, white, (0, 0, 9, 9))
        self.assertEqual(result["mae"], 1)
        self.assertEqual(result["rmse"], 1)
        self.assertAlmostEqual(result["ssim_luma"], 0.01**2 / (1 + 0.01**2), places=12)

    def test_shift_and_color_change_are_measured_without_alignment(self) -> None:
        with Image.new("RGB", (16, 16), "black") as reference:
            reference.paste((0, 0, 255), (2, 2, 7, 7))
            with Image.new("RGB", reference.size, "black") as shifted, reference.copy() as recolored:
                shifted.paste((0, 0, 255), (3, 2, 8, 7))
                recolored.paste((255, 0, 0), (2, 2, 7, 7))
                for candidate in (shifted, recolored):
                    result = region_metrics(reference, candidate, (0, 0, 16, 16))
                    self.assertGreater(result["mae"], 0)
                    self.assertLess(result["ssim_luma"], 1)

    def test_fixed_roi_ignores_changes_outside_it_and_small_roi_marks_ssim_unavailable(self) -> None:
        with Image.new("RGB", (16, 16), "black") as reference, reference.copy() as candidate:
            candidate.putpixel((15, 15), (255, 255, 255))
            result = region_metrics(reference, candidate, (0, 0, 4, 4))
        self.assertEqual(result["mae"], 0)
        self.assertIsNone(result["ssim_luma"])

    def test_ssim_matches_direct_sample_covariance_on_nonconstant_windows(self) -> None:
        with Image.new("RGB", (9, 8)) as left, Image.new("RGB", (9, 8)) as right:
            left.putdata([(n * 17 % 256,) * 3 for n in range(72)])
            right.putdata([(n * 13 % 256,) * 3 for n in range(72)])
            expected = []
            for y in range(2):
                for x in range(3):
                    a = [left.getpixel((i, j))[0] for j in range(y, y + 7) for i in range(x, x + 7)]
                    b = [right.getpixel((i, j))[0] for j in range(y, y + 7) for i in range(x, x + 7)]
                    mx, my = math.fsum(a) / 49, math.fsum(b) / 49
                    vx, vy = (math.fsum((v - mean) ** 2 for v in samples) / 48 for samples, mean in ((a, mx), (b, my)))
                    covariance = math.fsum((p - mx) * (q - my) for p, q in zip(a, b, strict=True)) / 48
                    expected.append(((2 * mx * my + 2.55**2) * (2 * covariance + 7.65**2)) / ((mx**2 + my**2 + 2.55**2) * (vx + vy + 7.65**2)))
            result = region_metrics(left, right, (0, 0, 9, 8))
        self.assertAlmostEqual(result["ssim_luma"], math.fsum(expected) / 6, places=11)

    def test_alpha_is_composited_over_white_and_non_png_rejected(self) -> None:
        with Image.new("RGBA", (8, 8), (0, 0, 0, 0)) as transparent, decode_png(png_bytes(transparent)) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255))
        with Image.new("RGB", (8, 8)) as image, BytesIO() as stream:
            image.save(stream, format="JPEG")
            with self.assertRaisesRegex(ValueError, "PNG"):
                decode_png(stream.getvalue())
        with self.assertRaisesRegex(ValueError, "PNG"):
            decode_png(b"not an image")

    def test_background_complement_counts_each_pixel_once_and_excludes_foreground(self) -> None:
        with Image.new("RGB", (16, 16), "black") as reference, reference.copy() as candidate:
            candidate.paste("white", (4, 4, 12, 12))
            result = background_metrics(reference, candidate, (4, 4, 12, 12))
            self.assertEqual(result["pixels"], 192)
            self.assertEqual(result["mae"], 0)
            candidate.putpixel((0, 0), (255, 255, 255))
            changed = background_metrics(reference, candidate, (4, 4, 12, 12))
        self.assertAlmostEqual(changed["mae"], 1 / 192)
        self.assertAlmostEqual(changed["rmse"], math.sqrt(1 / 192))
        self.assertIsNone(changed["ssim_luma"])


class ParameterReferenceTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "neutral.png"
        with Image.new("RGB", (1024, 576), "white") as image:
            self.png = png_bytes(image)
        self.digest = hashlib.sha256(self.png).hexdigest()
        self.path.write_bytes(self.png)

    def test_reference_identity_and_dimensions_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "frozen"):
            freeze_reference(self.path)
        with patch(MODULE + ".REFERENCE_SHA256", self.digest):
            spec = freeze_reference(self.path)
        self.assertEqual(spec.png, self.png)
        self.assertEqual(spec.sha256, self.digest)

    def test_mismatched_render_dimensions_rejected_without_resizing(self) -> None:
        spec = ReferenceSpec(self.png, self.digest, str(self.path))
        with Image.new("RGB", (32, 18)) as image:
            self.path.write_bytes(png_bytes(image))
        with patch(MODULE + ".REFERENCE_SHA256", self.digest), self.assertRaisesRegex(ValueError, "dimensions"):
            evaluate_render(spec, self.path)

    def test_frozen_reference_bytes_and_judge_pair_are_bound_without_path_leak(self) -> None:
        spec = ReferenceSpec(self.png, self.digest, "group-secret-reference")
        with patch(MODULE + ".REFERENCE_SHA256", self.digest):
            request = build_judge_request(spec, self.path)
            self.path.write_bytes(self.png + b"different encoded bytes")
            changed = build_judge_request(spec, self.path)
        self.assertNotEqual(request["pair_id"], changed["pair_id"])
        self.assertNotIn("group-secret", json.dumps(request["messages"]))
        self.assertNotIn(str(self.path), json.dumps(request["messages"]))
        with self.assertRaisesRegex(ValueError, "Reference bytes"):
            build_judge_request(ReferenceSpec(self.png + b"changed", self.digest, ""), self.path)


class ParameterJudgeTests(TestCase):
    def test_prompt_example_obeys_required_nested_criterion_contract(self) -> None:
        request = prepare_judge_request(b"reference", b"candidate", {"pair_id": PAIR})
        prompt = request["messages"][0]["content"][0]["text"]
        example = prompt.split("JSON shape example:\n", 1)[1].split("\nJSON schema=", 1)[0]
        result = parse_judge_result(example, PAIR)
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["overall"], "unassessed")
        self.assertEqual(len(result["criteria"]), 4)

    def test_legacy_evidence_arrays_without_criterion_status_remain_unassessed(self) -> None:
        value = valid_verdict()
        value["criteria"] = {name: criterion["evidence"] for name, criterion in value["criteria"].items()}
        result = parse_judge_result(json.dumps(value), PAIR)
        self.assertEqual(result["overall"], "unassessed")
        self.assertIsNotNone(result["parse_error"])

    def test_complete_consistent_verdict_is_assessed_without_claiming_acceptance(self) -> None:
        result = parse_judge_result(json.dumps(valid_verdict()), PAIR)
        self.assertEqual(result["overall"], "pass")
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["acceptance"], "uncalibrated")

    def test_missing_critical_field_empty_evidence_and_wrong_pair_are_unassessed(self) -> None:
        for mutate in (
            lambda value: value["criteria"].pop("edges_transitions"),
            lambda value: value["criteria"]["geometry_layout"].update(evidence=[]),
            lambda value: value.update(pair_id="b" * 64),
            lambda value: value["criteria"]["color_placement"].update(status="excellent"),
            lambda value: value["criteria"]["edges_transitions"].update(status="needs_work"),
            lambda value: value["criteria"]["edges_transitions"]["evidence"][0].update(rectangle=[0, 0, 1025, 576]),
            lambda value: value["criteria"]["edges_transitions"]["evidence"][0].update(observation="  "),
        ):
            value = valid_verdict()
            mutate(value)
            result = parse_judge_result(json.dumps(value), PAIR)
            self.assertEqual(result["overall"], "unassessed")
            self.assertIsNotNone(result["parse_error"])

    def test_non_json_duplicate_keys_nonfinite_and_extra_fields_are_unassessed(self) -> None:
        for raw in ("```json\n{}\n```", '{"pair_id":"a","pair_id":"b"}', '{"overall":NaN}', '{"overall":Infinity}', "[]", "null"):
            self.assertEqual(parse_judge_result(raw, PAIR)["overall"], "unassessed")
        value = valid_verdict()
        value["extra"] = "not allowed"
        self.assertEqual(parse_judge_result(json.dumps(value), PAIR)["overall"], "unassessed")

    def test_needs_work_and_unassessed_criteria_determine_overall(self) -> None:
        for status in ("needs_work", "unassessed"):
            value = valid_verdict()
            value["criteria"]["geometry_layout"]["status"] = status
            value["overall"] = status
            result = parse_judge_result(json.dumps(value), PAIR)
            self.assertEqual(result["overall"], status)
            self.assertIsNone(result["parse_error"])
