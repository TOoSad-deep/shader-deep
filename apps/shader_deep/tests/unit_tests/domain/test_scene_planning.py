"""整图 M/S 的覆盖、局部备选、组合来源与两类缺口."""

from __future__ import annotations

from copy import deepcopy
from unittest import TestCase

from pydantic import ValidationError, create_model

from shader_deep.domain.scene_observations import ObservationIssue, SceneObservations
from shader_deep.domain.scene_planning import (
    ScenePlanningSubmission,
    build_candidate_index,
    diagnose_scene_planning,
    effective_planning_sketch,
    validate_scene_planning,
)


def planning_observations() -> SceneObservations:
    return SceneObservations.model_validate(
        {
            "elements": [
                {"id": "E1", "name": "后圆", "region": "中央偏左下", "feature_ids": ["F1", "F2"]},
                {"id": "E2", "name": "前圆", "region": "右上", "feature_ids": ["F3"]},
            ],
            "features": [
                {"id": "F1", "description": "圆形轮廓"},
                {"id": "F2", "description": "青蓝渐变"},
                {"id": "F3", "description": "浅灰圆形外观"},
            ],
            "relations": [
                {"id": "R1", "participants": ["E1", "E2"], "description": "前圆覆盖后圆右上部分"},
                {"id": "R2", "participants": ["E1", "E2"], "description": "交叠区底色变软并偏紫"},
            ],
        }
    )


def planning_submission() -> ScenePlanningSubmission:
    return ScenePlanningSubmission.model_validate(
        {
            "mechanisms": [
                {"id": "M1", "name": "圆形遮罩", "method": "根据局部坐标、中心和半径输出平滑覆盖率。"},
                {"id": "M2", "name": "渐变填色", "method": "将坐标投影到方向, 插值颜色控制点, 输出颜色。"},
                {"id": "M3", "name": "多点柔化", "method": "对输入颜色函数多点求值并加权。", "conditions": ["底层颜色可独立重复求值。"]},
                {"id": "M4", "name": "解析柔化", "method": "扩大解析遮罩过渡并重新求值底色。"},
                {"id": "M5", "name": "纹理柔化", "method": "采样预滤波纹理。", "conditions": ["需要外部预滤波纹理。"]},
            ],
            "sketches": [
                {
                    "id": "S1",
                    "name": "解析叠圆",
                    "default": {"F1": ["M1"], "F2": ["M2"], "F3": ["M1"], "R1": ["M1"], "R2": ["M3", "M2"]},
                    "composition": "用 [[M1]] 与 [[M2]] 构造 [[E1]] 底色, 再用 [[M3]] 柔化。另用 [[M1]] 放置 [[E2]] 并混合。",
                    "alternatives": [
                        {
                            "choices": {"R2": ["M4", "M2"]},
                            "reason": "简单解析背景可近似。",
                            "conditions": ["底层可解析求值。"],
                            "composition": "用 [[M1]]、[[M2]] 构造后圆, 再用 [[M4]] 柔化后混合前圆。",
                        }
                    ],
                }
            ],
            "extra_candidates": [{"target": "R2", "mechanisms": ["M5", "M2"], "reason": "[[M5]] 所需外部纹理尚未提供。"}],
        }
    )


class ScenePlanningTests(TestCase):
    def setUp(self) -> None:
        self.observations = planning_observations()
        self.report = planning_submission()

    def test_complete_cross_element_default_and_alternative(self) -> None:
        validate_scene_planning(self.report, self.observations)
        selected = effective_planning_sketch(self.report, self.observations, "S1", alternative_index=0)
        self.assertEqual(selected.choices["R2"], ("M4", "M2"))
        self.assertEqual(selected.choices["F1"], ("M1",))
        self.assertNotIn("M3", {item.id for item in selected.mechanisms})
        self.assertNotIn("M5", {item.id for item in selected.mechanisms})
        self.assertEqual(selected.conditions, ("底层可解析求值。",))
        self.assertTrue(selected.complete)

    def test_candidate_combinations_retain_full_effective_origins(self) -> None:
        rows = build_candidate_index(self.report)
        target = next(row for row in rows if row.target == "F1")
        self.assertEqual([origin.alternative_index for origin in target.origins], [None, 0])
        groups = [row for row in rows if row.target == "R2"]
        self.assertEqual([row.mechanisms for row in groups], [("M3", "M2"), ("M4", "M2"), ("M5", "M2")])
        self.assertEqual(groups[-1].origins[0].extra_candidate_index, 0)

    def test_missing_target_requires_explicit_gap(self) -> None:
        payload = self.report.model_dump(mode="json")
        del payload["sketches"][0]["default"]["F3"]
        changed = ScenePlanningSubmission.model_validate(payload)
        self.assertIn("uncovered_target", {issue.code for issue in diagnose_scene_planning(changed, self.observations)})
        payload["sketches"][0]["gaps"] = [{"refs": ["F3"], "description": "前圆边界输入仍待明确"}]
        changed = ScenePlanningSubmission.model_validate(payload)
        selected = effective_planning_sketch(changed, self.observations, "S1")
        self.assertFalse(selected.complete)
        self.assertEqual(selected.gaps[0].refs, ("F3",))

    def test_selected_target_can_still_have_gap_and_alternative_inherits(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["sketches"][0]["gaps"] = [{"refs": ["R2"], "description": "共享底层函数仍有必要输入缺失"}]
        changed = ScenePlanningSubmission.model_validate(payload)
        selected = effective_planning_sketch(changed, self.observations, "S1", alternative_index=0)
        self.assertFalse(selected.complete)
        self.assertEqual(selected.gaps[0].refs, ("R2",))

    def test_upstream_element_and_global_gaps_remain_separate(self) -> None:
        for refs in ((), ("E2",), ("F3",), ("R2",)):
            upstream = (ObservationIssue(refs=refs, description="必要观察尚未确定"),)
            selected = effective_planning_sketch(self.report, self.observations, "S1", upstream_gaps=upstream)
            with self.subTest(refs=refs):
                self.assertFalse(selected.complete)
                self.assertEqual(selected.observation_gaps, upstream)
                self.assertEqual(selected.gaps, ())

    def test_background_only_allows_empty_mechanisms_and_choices(self) -> None:
        observations = SceneObservations(elements=(), features=(), relations=())
        report = ScenePlanningSubmission.model_validate(
            {"mechanisms": [], "sketches": [{"id": "S1", "name": "纯底色", "default": {}, "composition": "输出均匀浅灰背景。"}]}
        )
        self.assertTrue(effective_planning_sketch(report, observations, "S1").complete)
        self.assertFalse(
            effective_planning_sketch(report, observations, "S1", upstream_gaps=(ObservationIssue(description="必要主体尚未辨认"),)).complete
        )

    def test_invalid_choices_and_stale_inherited_composition_are_diagnosed(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["sketches"][0]["alternatives"][0].pop("composition")
        changed = ScenePlanningSubmission.model_validate(payload)
        issues = diagnose_scene_planning(changed, self.observations)
        self.assertTrue(any(issue.code == "invalid_body_reference" and "alternatives/0/composition" in issue.path for issue in issues))
        payload["sketches"][0]["alternatives"][0]["choices"]["F99"] = ["M1"]
        changed = ScenePlanningSubmission.model_validate(payload)
        self.assertIn("invalid_target", {issue.code for issue in diagnose_scene_planning(changed, self.observations)})

    def test_extra_candidate_cannot_be_adopted_only_by_composition(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["sketches"][0]["composition"] += " 另采用 [[M5]]。"
        with self.assertRaisesRegex(ValueError, "M5"):
            validate_scene_planning(ScenePlanningSubmission.model_validate(payload), self.observations)

    def test_duplicate_ids_references_and_unused_methods_have_locations(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["mechanisms"].append(payload["mechanisms"][0])
        payload["mechanisms"].append({"id": "M99", "name": "孤立方法", "method": "按输入阈值生成遮罩。"})
        payload["sketches"][0]["default"]["F1"] = ["M1", "M1"]
        issues = diagnose_scene_planning(ScenePlanningSubmission.model_validate(payload), self.observations)
        duplicate = next(issue for issue in issues if issue.code == "duplicate_id")
        self.assertEqual(duplicate.related_paths, ("/mechanisms/0/id", "/mechanisms/5/id"))
        self.assertIn("duplicate_reference", {issue.code for issue in issues})
        self.assertIn("unused_mechanism", {issue.code for issue in issues})

    def test_equal_candidate_combinations_ignore_order_and_require_manual_migration(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["extra_candidates"].append({"target": "R2", "mechanisms": ["M2", "M3"], "reason": "两方法需要统一坐标。"})
        issues = diagnose_scene_planning(ScenePlanningSubmission.model_validate(payload), self.observations)
        self.assertTrue(any(issue.code == "duplicate_candidate" and issue.path == "/extra_candidates/1" for issue in issues))

    def test_method_is_self_contained_but_ordinary_variable_names_are_allowed(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["mechanisms"][0]["method"] += " 使用普通变量 F1 计算。"
        validate_scene_planning(ScenePlanningSubmission.model_validate(payload), self.observations)
        payload["mechanisms"][0]["method"] += " 对 [[F1]] 求值。"
        self.assertIn(
            "invalid_body_reference",
            {issue.code for issue in diagnose_scene_planning(ScenePlanningSubmission.model_validate(payload), self.observations)},
        )

    def test_optional_composition_roundtrips_and_null_unknown_empty_gap_are_rejected(self) -> None:
        payload = self.report.model_dump(mode="json")
        payload["sketches"][0]["alternatives"][0].pop("composition")
        changed = ScenePlanningSubmission.model_validate(payload)
        self.assertEqual(ScenePlanningSubmission.model_validate(changed.model_dump(mode="json")), changed)
        bad = deepcopy(payload)
        bad["sketches"][0]["alternatives"][0]["composition"] = None
        with self.assertRaises(ValidationError):
            ScenePlanningSubmission.model_validate(bad)
        for update in ({"extra_candidates": None}, {"unknown": []}):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                ScenePlanningSubmission.model_validate({**payload, **update})
        payload["sketches"][0]["gaps"] = [{"refs": [], "description": "缺失"}]
        with self.assertRaises(ValidationError):
            ScenePlanningSubmission.model_validate(payload)

    def test_explicit_selection_validates_bounds(self) -> None:
        for selected in (-1, 1, True, "0"):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                effective_planning_sketch(self.report, self.observations, "S1", alternative_index=selected)
        with self.assertRaises(ValueError):
            effective_planning_sketch(self.report, self.observations, "S99")

    def test_tool_schema_exposes_optional_nonnullable_composition(self) -> None:
        alternative = ScenePlanningSubmission.model_json_schema()["$defs"]["PlanningAlternative"]
        composition = alternative["properties"]["composition"]
        self.assertEqual(composition["type"], "string")
        self.assertNotIn("anyOf", composition)
        self.assertNotIn("default", composition)
        self.assertNotIn("composition", alternative["required"])

    def test_alternative_serialization_preserves_python_and_json_modes(self) -> None:
        python_value = self.report.model_dump()
        alternative = python_value["sketches"][0]["alternatives"][0]
        self.assertEqual(alternative["choices"]["R2"], ("M4", "M2"))
        self.assertEqual(alternative["conditions"], ("底层可解析求值。",))
        self.assertEqual(ScenePlanningSubmission.model_validate(python_value), self.report)
        json_value = self.report.model_dump(mode="json")
        alternative = json_value["sketches"][0]["alternatives"][0]
        self.assertEqual(alternative["choices"]["R2"], ["M4", "M2"])
        self.assertEqual(alternative["conditions"], ["底层可解析求值。"])
        self.assertEqual(ScenePlanningSubmission.model_validate_json(self.report.model_dump_json()), self.report)
        arguments = create_model("PlanningToolArguments", report=(ScenePlanningSubmission, ...))(report=self.report)
        tool_payload = arguments.model_dump()["report"]
        self.assertEqual(tool_payload["sketches"][0]["alternatives"][0]["choices"]["R2"], ("M4", "M2"))
