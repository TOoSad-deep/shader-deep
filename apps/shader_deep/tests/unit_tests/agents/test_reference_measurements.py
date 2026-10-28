"""当前原图测量及历史证据读取共用的像素、统计与剖面合同."""

from __future__ import annotations

import io
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image
from pydantic import TypeAdapter, ValidationError

from shader_deep.domain.evidence import ImageRegion, MeasurementRecord, MeasurementRequest, MeasurementSpec
from shader_deep.imaging.measurements import ReferenceMeasurements
from shader_deep.imaging.profiles import summarize_profile


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

    def test_measurement_semantics_describe_actual_aggregation_and_legacy_absence(self) -> None:
        region, _ = self.reader.measure(self.request(measurement()))
        profile, _ = self.reader.measure(self.request(measurement("line_profile", right=8, axis="y")))
        self.assertEqual(region.metrics, ("mean_rgb", "mean_encoded_luma"))
        self.assertEqual(region.aggregation, "mean_over_all_pixels_in_region")
        self.assertEqual(region.samples_per_value, 24)
        self.assertEqual(profile.profile, (120.0,) * 6)
        self.assertEqual(profile.aggregation, "mean_over_each_row_in_region")
        self.assertEqual(profile.samples_per_value, 8)
        fields = {"metrics", "aggregation", "samples_per_value", "units", "limitations"}
        legacy = TypeAdapter(MeasurementRecord).validate_python({key: value for key, value in asdict(region).items() if key not in fields})
        self.assertEqual(legacy.metrics, ())
        self.assertIsNone(legacy.aggregation)
        self.assertIsNone(legacy.samples_per_value)

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
