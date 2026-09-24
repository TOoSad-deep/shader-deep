"""独立探索契约保护共享候选、范围和直接前提, 不验证视觉正确性."""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import cast

from pydantic import TypeAdapter, ValidationError

from shader_deep.analysis.exploration import (
    Candidate,
    CandidateRef,
    ElementScope,
    ExplorationReport,
    Feature,
    FeatureChoice,
    FinalFeatureChoice,
    FinalSketch,
    InstanceGroup,
    LibraryElement,
    OutlineElement,
    OutlineRelation,
    Participant,
    PossibilityLibrary,
    Relation,
    RelationChoice,
    Requirement,
    Sketch,
    VisualOutline,
    compact_data,
    validate_exploration,
    validate_library,
    validate_outline,
)
from shader_deep.analysis.schemas import AnalysisValidationError


def outline() -> VisualOutline:
    return VisualOutline(
        elements=(
            OutlineElement(
                id="E1",
                name="条带",
                scope="画面中部",
                salient_features=("边缘较亮",),
                instance_groups=(InstanceGroup(id="G1", description="左斜"), InstanceGroup(id="G2", description="右斜")),
            ),
            OutlineElement(id="E2", name="背景", scope="全图", salient_features=()),
        ),
        relations=(),
    )


def feature(identity: str = "F1") -> Feature:
    return Feature(
        id=identity,
        name="亮边",
        element_ids=("E1",),
        appearance="条带侧边更亮",
        candidates=(
            Candidate(id=identity + "-A", mechanism="边缘渐变", reasoning="控制亮边宽度"),
            Candidate(id=identity + "-B", mechanism="细亮线", reasoning="独立控制边缘强度"),
        ),
    )


def report() -> ExplorationReport:
    return ExplorationReport(
        sketch_library=(
            Sketch(
                id="S1",
                name="平面条带",
                element_ids=("E1",),
                composition=("重复模板",),
                feature_refs=(FeatureChoice(feature_id="F1", candidate_ids=("F1-A",)),),
            ),
        ),
        feature_library=(feature(),),
        relation_library=(),
    )


class ExplorationContractTests(unittest.TestCase):
    def test_compact_round_trip_preserves_required_empty_arrays(self) -> None:
        data = cast("dict[str, list[dict[str, object]]]", compact_data(outline()))
        self.assertEqual(TypeAdapter(VisualOutline).validate_python(data), outline())
        self.assertIsInstance(data, dict)
        self.assertEqual(data["relations"], [])
        self.assertEqual(data["elements"][1]["salient_features"], [])
        self.assertNotIn("instance_groups", data["elements"][1])
        empty = ExplorationReport(sketch_library=(), feature_library=(), relation_library=())
        self.assertEqual(compact_data(empty), {"sketch_library": [], "feature_library": [], "relation_library": []})

    def test_schema_rejects_missing_required_libraries_and_extra_old_fields(self) -> None:
        adapter = TypeAdapter(ExplorationReport)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"sketch_library": []})
        with self.assertRaises(ValidationError):
            adapter.validate_python({"sketch_library": [], "feature_library": [], "relation_library": [], "summary": "旧摘要"})
        with self.assertRaises(ValidationError):
            FeatureChoice(feature_id="F1", candidate_ids=("F1-A",), applies_to=())
        with self.assertRaises(ValidationError):
            CandidateRef(candidate_id="F1-A", priority_reason="未声明排序")

    def test_shared_candidates_and_unreferenced_candidates_remain_valid(self) -> None:
        initial = report()
        extra = replace(initial.sketch_library[0], id="S2", name="空间条带")
        independent = feature("F2")
        value = replace(initial, sketch_library=(*initial.sketch_library, extra), feature_library=(*initial.feature_library, independent))
        validate_exploration(value, outline())
        self.assertEqual(len(value.feature_library[0].candidates), 2)
        self.assertEqual(value.feature_library[1].id, "F2")

    def test_simple_composition_needs_no_feature_or_relation(self) -> None:
        value = ExplorationReport(
            sketch_library=(Sketch(id="S1", name="背景底色", element_ids=("E2",), composition=("均匀填色",)),),
            feature_library=(),
            relation_library=(),
        )
        validate_exploration(value, outline())

    def test_empty_exploration_cannot_masquerade_as_completion(self) -> None:
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_exploration(ExplorationReport(sketch_library=(), feature_library=(), relation_library=()), outline())
        self.assertEqual(caught.exception.issues[0]["code"], "empty_exploration")

    def test_outline_group_ownership_and_identity_are_checked(self) -> None:
        bad = OutlineRelation(participants=(ElementScope(element_id="E1"), ElementScope(element_id="E2", group_id="G1")), description="相邻")
        with self.assertRaisesRegex(AnalysisValidationError, "group_id"):
            validate_outline(replace(outline(), relations=(bad,)))
        duplicate = replace(outline().elements[1], id="G1")
        with self.assertRaisesRegex(AnalysisValidationError, "ID 必须唯一"):
            validate_outline(replace(outline(), elements=(outline().elements[0], duplicate)))

    def test_local_ids_are_unique_even_across_object_kinds(self) -> None:
        initial = report()
        duplicate = replace(initial.feature_library[0], candidates=(replace(initial.feature_library[0].candidates[0], id="S1"),))
        with self.assertRaisesRegex(AnalysisValidationError, "/feature_library/0/candidates/0/id"):
            validate_exploration(replace(initial, feature_library=(duplicate,)), outline())

    def test_choice_cannot_borrow_a_candidate_from_another_feature(self) -> None:
        initial = report()
        sketch = replace(initial.sketch_library[0], feature_refs=(FeatureChoice(feature_id="F1", candidate_ids=("F2-A",)),))
        with self.assertRaisesRegex(AnalysisValidationError, "候选不属于"):
            validate_exploration(replace(initial, feature_library=(feature(), feature("F2")), sketch_library=(sketch,)), outline())

    def test_scopes_can_narrow_multi_element_feature(self) -> None:
        initial = report()
        expanded = replace(feature(), element_ids=("E1", "E2"))
        with self.assertRaisesRegex(AnalysisValidationError, "草图之外"):
            validate_exploration(replace(initial, feature_library=(expanded,)), outline())
        choice = replace(initial.sketch_library[0].feature_refs[0], applies_to=(ElementScope(element_id="E1", group_id="G1"),))
        sketch = replace(initial.sketch_library[0], feature_refs=(choice,))
        validate_exploration(replace(initial, feature_library=(expanded,), sketch_library=(sketch,)), outline())

    def test_disjoint_group_choices_are_valid_but_overlapping_choices_fail(self) -> None:
        initial = report()
        first = FeatureChoice(feature_id="F1", candidate_ids=("F1-A",), applies_to=(ElementScope(element_id="E1", group_id="G1"),))
        second = FeatureChoice(feature_id="F1", candidate_ids=("F1-B",), applies_to=(ElementScope(element_id="E1", group_id="G2"),))
        sketch = replace(initial.sketch_library[0], feature_refs=(first, second))
        validate_exploration(replace(initial, sketch_library=(sketch,)), outline())
        sketch = replace(sketch, feature_refs=(first, replace(second, applies_to=None)))
        with self.assertRaisesRegex(AnalysisValidationError, "互不重叠"):
            validate_exploration(replace(initial, sketch_library=(sketch,)), outline())

    def test_dependency_requires_source_and_target_and_distinct_endpoints(self) -> None:
        initial = report()
        relation = Relation(
            id="R1",
            name="影响",
            kind="dependency",
            description="条带影响亮边",
            candidates=(Candidate(id="R1-A", mechanism="明暗作用", reasoning="待验证"),),
            participants=(Participant(kind="element", id="E1", role="source"), Participant(kind="feature", id="F1", role="source")),
        )
        with self.assertRaisesRegex(AnalysisValidationError, "同时包含源和目标"):
            validate_exploration(replace(initial, relation_library=(relation,)), outline())
        relation = replace(
            relation, participants=(Participant(kind="element", id="E1", role="source"), Participant(kind="element", id="E1", role="target"))
        )
        with self.assertRaisesRegex(AnalysisValidationError, "端点不能重复"):
            validate_exploration(replace(initial, relation_library=(relation,)), outline())

    def test_organization_cannot_carry_direction(self) -> None:
        relation = Relation(
            id="R1",
            name="交叉",
            kind="organization",
            description="两组相交",
            candidates=(Candidate(id="R1-A", mechanism="覆盖", reasoning="待验证"),),
            participants=(Participant(kind="element", id="E1", group_id="G1", role="source"), Participant(kind="element", id="E1", group_id="G2")),
        )
        with self.assertRaisesRegex(AnalysisValidationError, "组织关系不能携带方向"):
            validate_exploration(replace(report(), relation_library=(relation,)), outline())

    def test_relation_endpoints_need_explicit_sketch_references(self) -> None:
        initial = report()
        relation = Relation(
            id="R1",
            name="影响",
            kind="dependency",
            description="轮廓影响亮边",
            candidates=(Candidate(id="R1-A", mechanism="联动", reasoning="待验证"),),
            participants=(Participant(kind="feature", id="F2", role="source"), Participant(kind="feature", id="F1", role="target")),
        )
        sketch = replace(initial.sketch_library[0], relation_refs=(RelationChoice(relation_id="R1", candidate_ids=("R1-A",)),))
        with self.assertRaisesRegex(AnalysisValidationError, "全部端点"):
            validate_exploration(
                replace(initial, feature_library=(feature(), feature("F2")), relation_library=(relation,), sketch_library=(sketch,)), outline()
            )

    def test_direct_requirements_must_intersect_retained_candidates(self) -> None:
        initial = report()
        base = feature()
        needed = replace(base.candidates[0], requires=(Requirement(kind="feature", id="F2", candidate_ids=("F2-B",)),))
        dependent = replace(base, candidates=(needed, base.candidates[1]))
        choices = (*initial.sketch_library[0].feature_refs, FeatureChoice(feature_id="F2", candidate_ids=("F2-A",)))
        sketch = replace(initial.sketch_library[0], feature_refs=choices)
        value = replace(initial, feature_library=(dependent, feature("F2")), sketch_library=(sketch,))
        with self.assertRaisesRegex(AnalysisValidationError, "直接前提"):
            validate_exploration(value, outline())
        sketch = replace(sketch, feature_refs=(choices[0], replace(choices[1], candidate_ids=("F2-A", "F2-B"))))
        validate_exploration(replace(value, sketch_library=(sketch,)), outline())

    def test_requirement_cycles_are_not_rejected_as_unsatisfiable(self) -> None:
        first, second = feature(), feature("F2")
        first = replace(first, candidates=(replace(first.candidates[0], requires=(Requirement(kind="feature", id="F2", candidate_ids=("F2-A",)),)),))
        second = replace(
            second, candidates=(replace(second.candidates[0], requires=(Requirement(kind="feature", id="F1", candidate_ids=("F1-A",)),)),)
        )
        sketch = replace(
            report().sketch_library[0],
            feature_refs=(FeatureChoice(feature_id="F1", candidate_ids=("F1-A",)), FeatureChoice(feature_id="F2", candidate_ids=("F2-A",))),
        )
        validate_exploration(replace(report(), feature_library=(first, second), sketch_library=(sketch,)), outline())

    def test_relation_cycles_terminate_with_valid_endpoint_references(self) -> None:
        relations = tuple(
            Relation(
                id=identity,
                name="反馈",
                kind="dependency",
                description="可能反馈",
                candidates=(Candidate(id=identity + "-A", mechanism="反馈", reasoning="待验证"),),
                participants=(Participant(kind="relation", id=other, role="source"), Participant(kind="feature", id="F1", role="target")),
            )
            for identity, other in (("R1", "R2"), ("R2", "R1"))
        )
        sketch = replace(
            report().sketch_library[0],
            relation_refs=tuple(RelationChoice(relation_id=item.id, candidate_ids=(item.id + "-A",)) for item in relations),
        )
        validate_exploration(replace(report(), relation_library=relations, sketch_library=(sketch,)), outline())

    def test_final_priorities_and_unresolved_survive_compact_serialization(self) -> None:
        value = PossibilityLibrary(
            elements=(LibraryElement(id="E1", name="条带", scope="画面中部", unresolved=("厚度不明",)),),
            sketch_library=(
                FinalSketch(
                    id="S1",
                    name="平面",
                    element_ids=("E1",),
                    composition=("模板",),
                    feature_refs=(
                        FinalFeatureChoice(
                            feature_id="F1", candidate_refs=(CandidateRef(candidate_id="F1-A", priority="low"), CandidateRef(candidate_id="F1-B"))
                        ),
                    ),
                ),
            ),
            feature_library=(feature(),),
            relation_library=(),
        )
        validate_library(value)
        result = TypeAdapter(PossibilityLibrary).validate_python(compact_data(value))
        self.assertEqual(result, value)
        self.assertIsNone(result.sketch_library[0].feature_refs[0].candidate_refs[1].priority)
        self.assertEqual(result.elements[0].unresolved, ("厚度不明",))


if __name__ == "__main__":
    unittest.main()
