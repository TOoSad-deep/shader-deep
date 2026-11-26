"""参考标准整理、局部试验隔离和片段来源验证."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.local_probe import LocalProbeBinding, VerifiedSnippet
from shader_deep.domain.scene_planning import RiskProbe
from shader_deep.domain.visual_review import (
    DIMENSION_IDS,
    DimensionCriteria,
    PixelRegion,
    RequiredRegion,
    ReviewCoverage,
    ReviewRequest,
    bind_review_criteria,
)
from shader_deep.infrastructure.llm.budget import RequestBudget, TokenBalance, meter_requests
from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs, capture_planning_generation_inputs
from shader_deep.infrastructure.storage.planning_package import read_planning_package, write_planning_package
from shader_deep.workflows.planning.review_criteria import (
    NoLocalReviewChecksError,
    bind_local_probe,
    derive_local_review_request,
    draft_scene_review_criteria,
)
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.infrastructure.test_config import OPENROUTER
from tests.unit_tests.infrastructure.test_openrouter_transport import _response

if TYPE_CHECKING:
    import httpx2


def scene_task(*, source: str = "F8") -> ReviewRequest:
    dimensions = {
        "color": DimensionCriteria(id="color", required_checks=("保留后圆的渐变", "保留前圆填充"), source_refs=(source, "F15")),
        "relations": DimensionCriteria(id="relations", required_checks=("核对交叠变化",), source_refs=("R9",)),
    }
    return ReviewRequest(
        scope="scene",
        protocol="visual_review_v2",
        target="整图",
        requirements="复现参考图",
        dimensions=tuple(
            dimensions.get(identity) or DimensionCriteria(id=identity, excluded_checks=("本次无其他要求",)) for identity in DIMENSION_IDS
        ),
    )


class UpstreamReviewCriteriaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.package = read_planning_package(planning_report(self.root))
        self.target = PixelRegion(left=1, top=1, right=7, bottom=7)

    def test_organizer_uses_reference_only_and_rejects_unknown_observations(self) -> None:
        draft = scene_task()
        result = {"dimensions": [item.model_dump(mode="json") for item in draft.dimensions]}
        model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", result)])
        criteria = draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图", model=model)
        self.assertEqual(criteria, draft)
        self.assertEqual(len(model.requests), 1)
        message = model.requests[0][-1]
        self.assertEqual(set(json.loads(message.content[0]["text"])), {"observations", "context_notes", "user_request", "reference_size"})
        self.assertEqual(message.content[1]["image_url"]["url"], self.package.source.reference_url)
        invalid = scene_task(source="F99")
        model = NativeRoutingModel(
            responses=[call("SceneCriteriaDraft", {"dimensions": [item.model_dump(mode="json") for item in invalid.dimensions]})]
        )
        with self.assertRaisesRegex(ValueError, "本轮观察 ID"):
            draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图", model=model)

    def test_strict_organizer_binds_check_source_and_region_in_one_row(self) -> None:
        check = {"check": "核对参考色", "source_ref": "F8", "region": self.target.model_dump(), "tolerance": "每通道相差小于 2"}
        arguments = {identity: {"reason": "本次仅核对颜色"} for identity in DIMENSION_IDS if identity != "color"}
        arguments["color"] = {"checks": [check]}

        def respond(_client: httpx2.Client, request: httpx2.Request, **_kwargs: object) -> httpx2.Response:
            payload = json.loads(request.content)
            tool = payload["tools"][0]["function"]
            self.assertTrue(tool["strict"])
            self.assertEqual(set(tool["parameters"]["required"]), set(DIMENSION_IDS))
            choices = tool["parameters"]["properties"]["color"]["anyOf"]
            self.assertEqual({tuple(item["required"]) for item in choices}, {("checks",), ("reason",)})
            checks = next(item["properties"]["checks"] for item in choices if "checks" in item["properties"])
            self.assertEqual(checks["minItems"], 1)
            row = checks["items"]
            self.assertFalse(row["additionalProperties"])
            self.assertEqual(set(row["required"]), {"check", "source_ref", "region", "tolerance"})
            self.assertEqual(row["properties"]["check"]["minLength"], 1)
            self.assertNotIn("pattern", row["properties"]["check"])
            self.assertEqual(row["properties"]["source_ref"]["pattern"], r"^(?:[EFR][1-9][0-9]*|user_requirement)$")
            self.assertNotIn('"pattern": "\\\\S"', json.dumps(tool["parameters"]))
            return _response(
                request,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "organize", "type": "function", "function": {"name": "SceneCriteriaDraft", "arguments": json.dumps(arguments)}}
                    ],
                },
                streaming=False,
            )

        with patch.dict(os.environ, OPENROUTER, clear=True), patch("httpx2.Client.send", autospec=True, side_effect=respond) as transport:
            result = draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图")
            meter = RequestBudget(TokenBalance(16_384, None, 0, 0, 0, 0), None, lambda _: None)
            with meter_requests(meter):
                self.assertEqual(draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图"), result)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(meter.balance.attempts, 1)
        color = next(row for row in result.dimensions if row.id == "color")
        self.assertEqual((color.required_checks, color.source_refs, color.tolerances), (("核对参考色",), ("F8",), ("每通道相差小于 2",)))
        self.assertEqual(result.coverage.required_regions[0].region, self.target)
        for invalid in (
            {"content": arguments["content"], "layout": arguments["layout"]},
            {**arguments, "color": {"checks": []}},
            {**arguments, "color": {"checks": [{**check, "source_ref": "F99"}]}},
            {**arguments, "color": {"checks": [{**check, "check": " \t"}]}},
            {**arguments, "color": {"checks": [check], "reason": "不应同时排除"}},
        ):
            model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", invalid)])
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图", model=model)

    def test_local_criteria_retains_exact_checks_and_drops_unrelated_sources(self) -> None:
        scene = bind_review_criteria(scene_task(), "rules", "scene")
        probe = RiskProbe(target_ids=("E2",), reason="核对后圆", context_mode="isolated_solid")
        canvas = PixelRegion(left=0, top=0, right=32, bottom=24)
        request = derive_local_review_request(scene, probe, self.package.source.observations, self.target, canvas=canvas)
        local = bind_review_criteria(request, "rules", "element")
        self.assertEqual(local.request.target_kind, "element")
        self.assertEqual(local.checks, scene.checks[:1])
        color = next(item for item in request.dimensions if item.id == "color")
        self.assertEqual(color.excluded_checks, ("保留前圆填充",))
        self.assertEqual((request.coverage.reference_window, request.coverage.rendered_window), (canvas, canvas))
        self.assertEqual(request.coverage.required_regions[0].region, self.target)
        replaced = bind_review_criteria(scene_task(source="E2"), "rules", "scene")
        self.assertNotEqual(replaced.criteria_sha256, scene.criteria_sha256)

    def test_revision_adds_applicable_checks_and_retains_unrelated_manual_criteria(self) -> None:
        original = scene_task()
        color = next(row for row in original.dimensions if row.id == "color")
        original_relations = DimensionCriteria(id="relations", excluded_checks=("原先没有可比的遮挡",))
        canvas = PixelRegion(left=0, top=0, right=32, bottom=24)
        retained_region = RequiredRegion(dimension="color", check=color.required_checks[1], region=self.target)
        previous = original.model_copy(
            update={
                "dimensions": tuple(
                    color.model_copy(update={"tolerances": ("原人工容差", "共同容差")})
                    if row.id == "color"
                    else original_relations
                    if row.id == "relations"
                    else row
                    for row in original.dimensions
                ),
                "coverage": ReviewCoverage(reference_window=canvas, rendered_window=canvas, required_regions=(retained_region,)),
            }
        )
        proposed: dict[str, object] = {identity: {"reason": "本次未涉及"} for identity in DIMENSION_IDS}
        proposed["color"] = {"checks": [{"check": "核对后圆颜色", "source_ref": "F8", "region": None, "tolerance": "新颜色容差"}]}
        proposed["relations"] = {"checks": [{"check": "核对遮挡", "source_ref": "R9", "region": None, "tolerance": None}]}
        model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", proposed)])
        result = draft_scene_review_criteria(
            self.package.source.observation_dir, previous.requirements, model=model, previous=previous, affected_sources=("F8", "R9")
        )
        dimensions = {row.id: row for row in result.dimensions}
        self.assertEqual(dimensions["color"].required_checks, ("保留前圆填充", "核对后圆颜色"))
        self.assertEqual(dimensions["color"].tolerances, ("原人工容差", "共同容差", "新颜色容差"))
        self.assertEqual(dimensions["relations"].required_checks, ("核对遮挡",))
        self.assertEqual(dimensions["relations"].excluded_checks, ())
        self.assertEqual(result.coverage.required_regions, (retained_region,))
        self.assertEqual(dimensions["layout"], next(row for row in previous.dimensions if row.id == "layout"))

    def test_organizer_validates_coverage_and_local_probe_keeps_required_context(self) -> None:
        scene = scene_task()
        required = {"dimension": "relations", "check": "核对交叠变化", "region": {"left": 0, "top": 0, "right": 12, "bottom": 8}}
        response = {"dimensions": [item.model_dump(mode="json") for item in scene.dimensions], "required_regions": [required]}
        model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", response)])
        actual = draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图", model=model)
        frozen = bind_review_criteria(actual, "rules", "scene")
        probe = RiskProbe(target_ids=("R9",), reason="检验交互", context_mode="proxy_context")
        local = derive_local_review_request(frozen, probe, self.package.source.observations, self.target, canvas=actual.coverage.reference_window)
        self.assertEqual(local.coverage.required_regions, actual.coverage.required_regions)
        self.assertFalse(self.target.covers(local.coverage.required_regions[0].region))
        invalid = {**required, "region": {**required["region"], "right": 33}}
        model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", {**response, "required_regions": [invalid]})])
        with self.assertRaisesRegex(ValueError, "位于固定参考 PNG 内"):
            draft_scene_review_criteria(self.package.source.observation_dir, "复现参考图", model=model)

    def test_context_binding_and_verified_snippet_require_specific_sources(self) -> None:
        scene = bind_review_criteria(scene_task(), "rules", "scene")
        plan = self.package
        for mode, context in (
            ("isolated_solid", {"solid_rgba": (0.0, 0.0, 0.0, 1.0)}),
            ("proxy_context", {"proxy_context": "后圆作为代理底色"}),
            ("scene_baseline_patch", {"baseline_glsl": "head; fragment; tail", "patch_source": "fragment"}),
        ):
            with self.subTest(mode=mode):
                probe = RiskProbe(target_ids=("R9",), reason="核对交叠", context_mode=mode)
                binding = bind_local_probe(plan, "S1", probe, scene, self.target, **context)
                self.assertEqual(binding.planning_sha256, plan.content_sha256)
                self.assertEqual((binding.width, binding.height, binding.time), (32, 24, 1.5))
                self.assertEqual(binding.mechanism_ids, ("M3",))
                self.assertEqual(binding.criteria.checks[0].source_ref, "R9")
                mutable = {"u_gain": 0.25}
                snippet = VerifiedSnippet(
                    binding=binding,
                    glsl_code="void mainImage(...) {}",
                    uniforms=mutable,
                    preview_png=plan.source.reference_bytes,
                    review_criteria_sha256=binding.criteria.criteria_sha256,
                    review_report_sha256="a" * 64,
                )
                mutable["u_gain"] = 0.75
                self.assertEqual(snippet.metadata()["uniforms"], {"u_gain": 0.25})
        with self.assertRaisesRegex(ValueError, "精确出现一次"):
            LocalProbeBinding.model_validate({**binding.model_dump(), "baseline_glsl": "fragment; fragment"})

    def test_removing_all_checks_from_one_target_marks_its_probe_obsolete(self) -> None:
        scene = bind_review_criteria(scene_task(source="F3"), "rules", "scene")
        probe = RiskProbe(target_ids=("F8",), reason="检验渐变", context_mode="proxy_context")
        with self.assertRaises(NoLocalReviewChecksError):
            derive_local_review_request(
                scene, probe, self.package.source.observations, self.target, canvas=PixelRegion(left=0, top=0, right=32, bottom=24)
            )

    def _verified(self) -> VerifiedSnippet:
        scene = bind_review_criteria(scene_task(), "rules", "scene")
        probe = RiskProbe(target_ids=("F8",), reason="检验渐变", context_mode="isolated_solid")
        binding = bind_local_probe(self.package, "S1", probe, scene, self.target, solid_rgba=(0.0, 0.0, 0.0, 1.0))
        return VerifiedSnippet(
            binding=binding,
            glsl_code="void mainImage(out vec4 c, in vec2 p) { c = vec4(1.0); }",
            uniforms={},
            preview_png=self.package.source.reference_bytes,
            review_criteria_sha256=binding.criteria.criteria_sha256,
            review_report_sha256="a" * 64,
        )

    def _scene(self, directory: Path, snippet: VerifiedSnippet, *, time: float = 1.5) -> CapturedPlanningInputs:
        run = Path(tempfile.mkdtemp(dir=self.root))
        return capture_planning_generation_inputs(
            directory, run, "S1", options=GenerationOptions(width=32, height=24, time=time), verified_snippets=(snippet,)
        )

    def test_public_capture_rejects_foreign_reference_and_wrong_frame(self) -> None:
        snippet = self._verified()
        foreign = planning_report(self.root / "foreign", color=(0, 0, 0, 255))
        with self.assertRaisesRegex(ValueError, "参考图、目标方法依赖或渲染条件"):
            self._scene(foreign, snippet)
        with self.assertRaisesRegex(ValueError, "参考图、目标方法依赖或渲染条件"):
            self._scene(self.package.directory, snippet, time=0.5)
        legacy = replace(snippet, binding=snippet.binding.model_copy(update={"dependency_sha256": None}))
        self.assertEqual(self._scene(self.package.directory, legacy).verified_snippets[0].code, snippet.glsl_code)

    def test_public_capture_reuses_only_unchanged_target_methods(self) -> None:
        snippet = self._verified()
        report = self.package.report
        for method_id in ("M2", "M3"):
            changed = tuple(item.model_copy(update={"method": "改用分段插值。"}) if item.id == method_id else item for item in report.mechanisms)
            directory = write_planning_package(self.root / method_id, self.package.source, report.model_copy(update={"mechanisms": changed}))
            if method_id == "M2":
                with self.assertRaisesRegex(ValueError, "目标方法依赖"):
                    self._scene(directory, snippet)
            else:
                self.assertEqual(self._scene(directory, snippet).verified_snippets[0].code, snippet.glsl_code)
                patch_probe = RiskProbe(target_ids=("F8",), reason="在整图内检验", context_mode="scene_baseline_patch")
                patch_binding = bind_local_probe(
                    self.package,
                    "S1",
                    patch_probe,
                    bind_review_criteria(scene_task(), "rules", "scene"),
                    self.target,
                    baseline_glsl=snippet.glsl_code,
                    patch_source="vec4(1.0)",
                )
                patch_snippet = replace(snippet, binding=patch_binding, review_criteria_sha256=patch_binding.criteria.criteria_sha256)
                with self.assertRaisesRegex(ValueError, "目标方法依赖"):
                    self._scene(directory, patch_snippet)
        sketch = report.sketches[0]
        changed_sketch = sketch.model_copy(update={"default": {**sketch.default, "F8": ("M1",)}})
        directory = write_planning_package(
            self.root / "change-method", self.package.source, report.model_copy(update={"sketches": (changed_sketch,)})
        )
        with self.assertRaisesRegex(ValueError, "目标方法依赖"):
            self._scene(directory, snippet)
