from __future__ import annotations

import io
import json
import unittest
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from PIL import Image
from pydantic import ValidationError

from shader_deep.domain.shader_parameters import ParameterSpec, bake_uniforms, resolve_uniforms
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.optimization import ProtectedRegion, optimize_uniforms
from shader_deep.optimization.records import selected_optimization
from shader_deep.optimization.search import _minimize

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from shader_deep.domain.shader_parameters import UniformValue
    from shader_deep.optimization.objective import RegionBox
    from shader_deep.optimization.search import Point


def _encode(image: Image.Image) -> bytes:
    with io.BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


def _spec(default: float = 0.25, minimum: float = 0.0, maximum: float = 1.0) -> ParameterSpec:
    return ParameterSpec(name="u_brightness", type="float", default=default, min=minimum, max=maximum)


_CODE = "uniform float u_brightness;\nvoid mainImage(out vec4 c, in vec2 p) { c = vec4(u_brightness); }"
_TOP = PixelRegion(left=0, top=0, right=8, bottom=4)
_BOTTOM = PixelRegion(left=0, top=4, right=8, bottom=8)


class _SyntheticRenderer:
    def __init__(self, *, linked_halves: bool = False) -> None:
        self.linked_halves = linked_halves
        self.reads: list[dict[str, UniformValue]] = []
        self.exports: list[dict[str, UniformValue]] = []
        self.sessions = 0
        self.active = False

    @contextmanager
    def optimization_session(
        self, glsl_code: str, width: int, height: int, time: float, *, uniforms: Mapping[str, UniformValue]
    ) -> Iterator[_SyntheticRenderer]:
        self.sessions += 1
        self.active = True
        self.conditions = glsl_code, width, height, time
        self.initial = dict(uniforms)
        try:
            yield self
        finally:
            self.active = False

    def image(self, uniforms: Mapping[str, UniformValue]) -> Image.Image:
        value = uniforms["u_brightness"]
        if not isinstance(value, float):
            msg = "Expected scalar brightness"
            raise TypeError(msg)
        brightness = round(value * 255)
        bottom = brightness if self.linked_halves else 128
        image = Image.new("RGBA", (8, 8), (bottom, bottom, bottom, 255))
        image.paste((brightness, brightness, brightness, 255), (0, 0, 8, 4))
        return image

    def read_regions(self, uniforms: Mapping[str, UniformValue], regions: Sequence[RegionBox]) -> tuple[bytes, ...]:
        self.reads.append(dict(uniforms))
        with self.image(uniforms) as image:
            pixels = []
            for region in regions:
                with image.crop(region) as cropped:
                    pixels.append(cropped.tobytes())
            return tuple(pixels)

    def render(self, uniforms: Mapping[str, UniformValue]) -> bytes:
        self.exports.append(dict(uniforms))
        with self.image(uniforms) as image:
            return _encode(image)


class ParameterTests(unittest.TestCase):
    def test_contracts_and_baking_support_legacy_scalar_vectors_and_comments(self) -> None:
        spec = ParameterSpec(name="u_rgb", type="vec3", default=(0.0, 0.5, 1.0), min=(0.0, 0.0, 0.0), max=(1.0, 1.0, 1.0))
        values = resolve_uniforms([_spec(), spec], {"u_rgb": [0.25, 0.75, 0.5]})
        code = "#define ENABLE_RGB 1\n// uniform vec3 u_missing;\nuniform highp vec3 /* color */ u_rgb;\n" + _CODE
        baked = bake_uniforms(code, [_spec(), spec], values)
        self.assertIn("#define ENABLE_RGB 1", baked)
        self.assertIn("// uniform vec3 u_missing;", baked)
        self.assertIn("const highp vec3 /* color */ u_rgb = vec3(0.25, 0.75, 0.5);", baked)
        self.assertIn("const float u_brightness = 0.25;", baked)
        plain = "// uniform float u_unused;\nvoid mainImage(out vec4 c, in vec2 p) { c = vec4(iTime); }"
        self.assertEqual(bake_uniforms(plain, []), plain)
        with self.assertRaisesRegex(ValueError, "matching ParameterSpec"):
            bake_uniforms(_CODE, [])

    def test_contract_rejects_reserved_precision_shape_range_and_fake_declarations(self) -> None:
        for args in (
            {"name": "iTime"},
            {"name": "u__reserved"},
            {"default": float("nan")},
            {"default": True},
            {"default": (0.5,)},
            {"min": 0.75, "max": 0.25},
            {"max": 1e100},
        ):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                ParameterSpec.model_validate(_spec().model_dump() | args)
        for code in ("// " + _CODE, _CODE.replace("uniform float", "uniform vec2"), _CODE + "uniform float u_another;"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                bake_uniforms(code, [_spec()])
        for values in ({"u_unknown": 0.2}, {"u_brightness": 1.2}, {"u_brightness": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_uniforms([_spec()], values)

    def test_baking_rejects_qualified_and_non_single_uniform_declarations(self) -> None:
        for declaration in (
            "layout(location = 0) uniform float u_brightness;",
            "layout(location = (0 + 0)) /* keep line */\n uniform float u_brightness;",
            "highp uniform float u_brightness;",
            "uniform float u_brightness, other;",
            "uniform float u_brightness[1];",
            "#define PARAM uniform float u_brightness;",
        ):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, "standalone declaration"):
                bake_uniforms(declaration + "\n" + _CODE.split("\n", 1)[1], [_spec()])


class OptimizationTests(unittest.TestCase):
    def test_baseline_already_matches_reference_and_skips_remaining_trials(self) -> None:
        renderer = _SyntheticRenderer()
        baseline = {"u_brightness": 0.25}
        with renderer.image(baseline) as image:
            reference = _encode(image)
        result = optimize_uniforms(renderer, _CODE, reference, 8, 8, 0.0, specs=[_spec()])
        self.assertEqual(result.status, "baseline_satisfied")
        self.assertEqual(result.start_error, 0.0)
        self.assertEqual(result.end_error, 0.0)
        self.assertEqual(result.evaluations, 1)
        self.assertEqual(result.uniforms, baseline)
        self.assertEqual(result.baseline_render, reference)
        self.assertIsNone(result.best_render)
        self.assertEqual(renderer.reads, [baseline])
        self.assertEqual(renderer.exports, [baseline])

    def test_already_satisfied_record_is_available_for_selected_baseline(self) -> None:
        record = {
            "status": "baseline_satisfied",
            "baseline_candidate_id": "baseline",
            "start_error": 0.0,
            "end_error": 0.0,
            "best_observed_error": 0.0,
            "uniforms": {"u_brightness": 0.25},
            "evaluations": 1,
            "rejected_trials": 0,
        }
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "optimization-001.json").write_text(json.dumps(record), encoding="utf-8")
            result = selected_optimization(directory, "baseline")
            self.assertIsNone(selected_optimization(directory, "elsewhere"))
        if result is None:
            self.fail("Expected the saved baseline optimization")
        self.assertEqual(result.status, "baseline_satisfied")
        self.assertEqual(result.uniforms, record["uniforms"])

    def test_gray16_reference_targets_midgray(self) -> None:
        with Image.new("I;16", (8, 8), 32768) as image:
            reference = _encode(image)
        result = optimize_uniforms(
            _SyntheticRenderer(), _CODE, reference, 8, 8, 0.0, specs=[_spec()], regions=[_TOP], anchors=[_TOP], max_evaluations=32
        )
        self.assertEqual(result.status, "improved")
        self.assertAlmostEqual(cast("float", result.uniforms["u_brightness"]), 128 / 255, delta=0.01)

    def test_search_exports_protected_intermediate_improvements_and_winner(self) -> None:
        renderer = _SyntheticRenderer()
        with renderer.image({"u_brightness": 0.74}) as image:
            reference = _encode(image)
        result = optimize_uniforms(
            renderer, _CODE, reference, 8, 8, 2.5, specs=[_spec()], regions=[_TOP], anchors=[_TOP, _BOTTOM], max_evaluations=20
        )
        self.assertEqual(result.status, "improved")
        self.assertLess(result.end_error, result.start_error)
        value = result.uniforms["u_brightness"]
        self.assertIsInstance(value, float)
        self.assertAlmostEqual(cast("float", value), 0.74, delta=0.01)
        self.assertEqual(len(renderer.reads), result.evaluations)
        self.assertLessEqual(result.evaluations, 20)
        self.assertEqual(renderer.sessions, 1)
        self.assertEqual(renderer.conditions, (_CODE, 8, 8, 2.5))
        self.assertGreaterEqual(len(result.turning_points), 1)
        self.assertLessEqual(len(result.turning_points), 2)
        self.assertEqual(renderer.exports, [{"u_brightness": 0.25}, result.uniforms, *(point.uniforms for point in result.turning_points)])
        for point in result.turning_points:
            self.assertLess(result.end_error, point.error)
            self.assertLess(point.error, result.start_error)
            self.assertEqual(point.uniforms, renderer.reads[point.evaluation - 1])
            with renderer.image(point.uniforms) as expected:
                self.assertEqual(point.png, _encode(expected))
        self.assertIsNotNone(result.best_render)
        self.assertFalse(renderer.active)

    def test_search_rejects_every_improvement_that_breaks_protected_pixels(self) -> None:
        renderer = _SyntheticRenderer(linked_halves=True)
        with renderer.image({"u_brightness": 0.9}) as image:
            reference = _encode(image)
        result = optimize_uniforms(
            renderer, _CODE, reference, 8, 8, 0.0, specs=[_spec()], regions=[_TOP], protected_regions=[ProtectedRegion(region=_BOTTOM)]
        )
        self.assertEqual(result.status, "structure_bottleneck")
        self.assertGreater(result.rejected_trials, 0)
        self.assertEqual(result.start_error, result.end_error)
        self.assertEqual(result.uniforms, {"u_brightness": 0.25})
        self.assertEqual(renderer.exports, [result.uniforms])
        self.assertIsNone(result.best_render)

    def test_boundary_and_whitelist_are_enforced_before_opening_session(self) -> None:
        renderer = _SyntheticRenderer()
        with renderer.image({"u_brightness": 1.0}) as image:
            reference = _encode(image)
        result = optimize_uniforms(renderer, _CODE, reference, 8, 8, 0.0, specs=[_spec(maximum=0.5)])
        self.assertEqual(result.uniforms, {"u_brightness": 0.5})
        self.assertEqual(result.boundary_parameters, ("u_brightness",))
        with self.assertRaisesRegex(ValueError, "declared"):
            optimize_uniforms(renderer, _CODE, reference, 8, 8, 0.0, specs=[_spec()], parameter_names=["u_missing"])
        with self.assertRaisesRegex(ValueError, "same width and height"):
            optimize_uniforms(renderer, _CODE, reference, 7, 8, 0.0, specs=[_spec()])
        self.assertEqual(renderer.sessions, 1)

    def test_covariance_search_is_repeatable_on_a_coupled_nine_dimensional_goal(self) -> None:
        origin = (0.2,) * 9

        def objective(values: Point) -> float:
            components = sum((value - 0.7) ** 2 for value in values)
            coupling = 2 * sum((left - right) ** 2 for left, right in pairwise(values))
            return components + coupling

        first = _minimize(objective, origin, 100)
        second = _minimize(objective, origin, 100)
        self.assertEqual(first.best, second.best)
        self.assertLess(first.error, objective(origin) / 2)
        self.assertLessEqual(len(first.cache), 100)
        self.assertTrue(all(0 <= value <= 1 for point in first.cache for value in point))
