"""全图观察的主归属、引用和粒度合同."""

from __future__ import annotations

from unittest import TestCase

from pydantic import ValidationError

from shader_deep.domain.scene_observations import (
    ObservationFeature,
    ObservationIssue,
    ObservationRelation,
    SceneObservations,
    SceneObservationSubmission,
    build_scene_observations,
    validate_scene_observations,
)


def scene_submission() -> SceneObservationSubmission:
    return SceneObservationSubmission.model_validate(
        {
            "context_notes": ["浅灰背景"],
            "elements": [
                {
                    "id": "E2",
                    "name": "后方圆",
                    "region": "左下",
                    "features": [
                        {"id": "F8", "description": "青蓝色场 F8, 不要求正文引用标记"},
                        {"id": "F3", "description": "圆形边界"},
                    ],
                },
                {
                    "id": "E7",
                    "name": "前方圆",
                    "region": "右上",
                    "features": [
                        {"id": "F15", "description": "低对比灰色填充与圆形边界"},
                    ],
                },
            ],
            "relations": [
                {"id": "R9", "participants": ["E7", "E2", "F8"], "description": "前方覆盖后方并改变底层色场"},
                {"id": "R2", "participants": ["F8", "F3"], "description": "同元素内色场贴合轮廓"},
            ],
            "open_questions": [{"refs": ["R9"], "description": "交叠区是否存在位移尚不明确"}],
        }
    )


class SceneObservationTests(TestCase):
    def setUp(self) -> None:
        self.report = scene_submission()
        self.scene = build_scene_observations(self.report)

    def test_multiple_elements_keep_original_ids_order_and_main_ownership(self) -> None:
        self.assertEqual([item.id for item in self.scene.elements], ["E2", "E7"])
        self.assertEqual([item.id for item in self.scene.features], ["F8", "F3", "F15"])
        self.assertEqual(self.scene.elements[0].feature_ids, ("F8", "F3"))
        self.assertEqual(self.scene.elements[1].feature_ids, ("F15",))
        self.assertEqual(self.scene.relations[0].participants, ("E7", "E2", "F8"))
        self.assertIn("F8", self.scene.features[0].description)

    def test_background_only_requires_a_real_nonblank_context_note(self) -> None:
        background = SceneObservationSubmission(elements=(), context_notes=("白色纯色背景",))
        self.assertEqual(build_scene_observations(background), SceneObservations(elements=(), features=(), relations=()))
        with self.assertRaises(ValueError):
            build_scene_observations(SceneObservationSubmission(elements=()))
        with self.assertRaises(ValidationError):
            SceneObservationSubmission(elements=(), context_notes=("  ",))

    def test_input_is_frozen_and_nested_tuple_values_are_immutable(self) -> None:
        self.assertIsInstance(self.report.elements, tuple)
        self.assertIsInstance(self.report.elements[0].features, tuple)
        with self.assertRaises(ValidationError):
            self.report.elements[0].features[0].id = "F2"

    def test_invalid_feature_identity_and_planning_fields_are_rejected(self) -> None:
        for identity in ("F0", "E1", "F01", "f1", 1):
            with self.subTest(identity=identity), self.assertRaises(ValidationError):
                ObservationFeature.model_validate({"id": identity, "description": "轮廓"})
        with self.assertRaises(ValidationError):
            ObservationFeature.model_validate({"id": "F1", "description": "轮廓", "mechanism_refs": []})

    def test_duplicate_id_across_elements_and_within_one_element_is_rejected(self) -> None:
        for second in (self.report.elements[0], self.report.elements[1].model_copy(update={"features": (self.report.elements[0].features[0],)})):
            with self.subTest(second=second), self.assertRaises(ValueError):
                build_scene_observations(self.report.model_copy(update={"elements": (self.report.elements[0], second)}))
        first = self.report.elements[0].model_copy(update={"features": (self.report.elements[0].features[0],) * 2})
        with self.assertRaises(ValueError):
            build_scene_observations(self.report.model_copy(update={"elements": (first,)}))

    def test_relation_requires_distinct_existing_element_or_feature_participants(self) -> None:
        for participants in (("E2", "E2"), ("E2", "E99"), ("F8", "R2")):
            relation = ObservationRelation(id="R1", participants=participants, description="局部交互")
            with self.subTest(participants=participants), self.assertRaises(ValueError):
                build_scene_observations(self.report.model_copy(update={"relations": (relation,), "open_questions": ()}))

    def test_issue_refs_accept_relations_and_whole_scene_but_reject_missing_or_repeated_refs(self) -> None:
        for refs in ((), ("R9",), ("E7", "F15")):
            issue = ObservationIssue(refs=refs, description="必要观察缺口")
            self.assertEqual(build_scene_observations(self.report.model_copy(update={"gaps": (issue,)})), self.scene)
        for refs in (("missing",), ("F8", "F8")):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                build_scene_observations(self.report.model_copy(update={"gaps": (ObservationIssue(refs=refs, description="缺口"),)}))

    def test_flat_reader_contract_rejects_orphan_shared_and_missing_feature_ownership(self) -> None:
        shared = self.scene.elements[1].model_copy(update={"feature_ids": ("F8", "F15")})
        missing = self.scene.elements[1].model_copy(update={"feature_ids": ("F99",)})
        orphan = self.scene.elements[0].model_copy(update={"feature_ids": ("F8",)})
        for elements in ((self.scene.elements[0], shared), (self.scene.elements[0], missing), (orphan, self.scene.elements[1])):
            with self.subTest(elements=elements), self.assertRaises(ValueError):
                validate_scene_observations(self.scene.model_copy(update={"elements": elements}))

    def test_validation_rechecks_model_copy_changes_to_field_contracts(self) -> None:
        feature = self.scene.features[0].model_copy(update={"id": "R1"})
        with self.assertRaises(ValidationError):
            validate_scene_observations(self.scene.model_copy(update={"features": (feature,)}))
