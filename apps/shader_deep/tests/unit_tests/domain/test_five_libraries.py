from __future__ import annotations

import unittest

from pydantic import ValidationError

from shader_deep.domain.five_libraries import (
    Alternative,
    Element,
    ExplorationSubmission,
    Feature,
    Issue,
    LibraryValidationError,
    Mechanism,
    MechanismRef,
    MergeGroup,
    MergeProposal,
    Participant,
    Relation,
    Sketch,
    apply_merges,
    collect_explorations,
    exploration_mappings,
    merge_mapping,
    parse_references,
    resolve_mapping,
    rewrite_issues,
    rewrite_text,
    validate_exploration,
    validate_libraries,
)


def target() -> Element:
    return Element(id="E1", name="圆形", region="中央主体", feature_ids=())


def report() -> ExplorationSubmission:
    return ExplorationSubmission(
        features=(Feature(id="F1", description="圆形轮廓", mechanism_refs=(MechanismRef(id="M1"),)),),
        relations=(),
        mechanisms=(Mechanism(id="M1", name="距离场", method="计算坐标到中心的距离, 输出遮罩"),),
        sketches=(Sketch(id="S1", element_id="E1", name="分层", default={"F1": ("M1",)}, composition="沿 [[F1]] 构建遮罩"),),
        open_questions=(Issue(refs=("F1",), description="[[F1]] 的边缘柔度不确定"),),
    )


def proposal(kind: str, ids: tuple[str, ...], keep: str) -> MergeProposal:
    return MergeProposal.model_validate({"groups": [{"kind": kind, "ids": ids, "keep": keep}]})


class FiveLibrariesTests(unittest.TestCase):
    def test_required_text_rejects_blank_scope_and_name_without_trimming(self) -> None:
        for field in ("id", "name", "region"):
            for blank in ("", " ", "\t\n", "\u3000"):
                with self.subTest(field=field, blank=blank), self.assertRaises(ValidationError):
                    Element.model_validate({**target().model_dump(), field: blank})
        element = target().model_copy(update={"name": " 圆形 ", "region": " 中央主体 "})
        validated = Element.model_validate(element.model_dump())
        self.assertEqual(validated.name, " 圆形 ")
        self.assertEqual(validated.region, " 中央主体 ")
        with self.assertRaises(ValidationError):
            Feature(id="F1", description="\n  ", mechanism_refs=())

    def test_source_mappings_match_collected_ids_and_accepted_merges(self) -> None:
        reports = {"worker-a": report(), "worker-b": report()}
        mappings = exploration_mappings(target(), reports)
        self.assertEqual(mappings["worker-a"], {"E1": "E1", "F1": "F1", "M1": "M1", "S1": "S1"})
        self.assertEqual(mappings["worker-b"], {"E1": "E1", "F1": "F2", "M1": "M2", "S1": "S2"})
        library, _, _ = collect_explorations(target(), reports)
        for number, source in enumerate(reports):
            self.assertEqual(library.features[number].id, mappings[source]["F1"])
            self.assertEqual(library.mechanisms[number].id, mappings[source]["M1"])
            self.assertEqual(library.sketches[number].id, mappings[source]["S1"])
            self.assertEqual(library.sketches[number].default, {mappings[source]["F1"]: (mappings[source]["M1"],)})
        merges = proposal("features", ("F1", "F2"), "F1")
        accepted = apply_merges(library, merges, "E1")
        merge_ids = merge_mapping(library, merges)
        final = {source: {local: merge_ids.get(short, short) for local, short in ids.items()} for source, ids in mappings.items()}
        self.assertEqual(final["worker-b"]["F1"], accepted.features[0].id)
        self.assertEqual(final["worker-b"]["M1"], "M2")

    def test_source_collisions_preserve_sketches_and_bind_features(self) -> None:
        library, questions, gaps = collect_explorations(target(), {"a": report(), "b": report()})
        self.assertEqual(library.elements[0].feature_ids, ("F1", "F2"))
        self.assertEqual([item.id for item in library.sketches], ["S1", "S2"])
        self.assertEqual(library.sketches[1].default, {"F2": ("M2",)})
        self.assertEqual(library.sketches[1].composition, "沿 [[F2]] 构建遮罩")
        self.assertEqual(questions[1].refs, ("F2",))
        self.assertEqual(questions[1].description, "[[F2]] 的边缘柔度不确定")
        self.assertEqual(gaps, ())

    def test_no_sketch_minimum_is_introduced(self) -> None:
        submission = report().model_copy(update={"sketches": ()})
        validate_exploration(submission, target())
        library, _, _ = collect_explorations(target(), {"a": submission})
        self.assertEqual(library.sketches, ())

    def test_empty_observation_baseline_is_rejected(self) -> None:
        empty = ExplorationSubmission(features=(), relations=(), mechanisms=(), sketches=(), gaps=(Issue(refs=(), description="无法观察"),))
        validate_exploration(empty, target())
        with self.assertRaisesRegex(LibraryValidationError, "可用基线"):
            collect_explorations(target(), {"a": empty})

    def test_empty_candidates_need_a_gap(self) -> None:
        submission = report().model_copy(update={"features": (Feature(id="F1", description="边缘", mechanism_refs=()),), "sketches": ()})
        with self.assertRaisesRegex(LibraryValidationError, "对应缺口"):
            validate_exploration(submission, target())
        validate_exploration(submission.model_copy(update={"gaps": (Issue(refs=("F1",), description="未找到方法"),)}), target())

    def test_unknown_marker_and_unmarked_id_have_field_context(self) -> None:
        for composition in ("使用 [[F9]]", "沿 F1 裁切", "使用 [[]]", "[[F1\n]]", "[[ F1 ]]", "[[[[F1]]]]"):
            with self.subTest(composition=composition):
                sketch = report().sketches[0].model_copy(update={"composition": composition})
                with self.assertRaises(LibraryValidationError) as caught:
                    validate_exploration(report().model_copy(update={"sketches": (sketch,)}), target())
                self.assertIn("composition", caught.exception.field)
                self.assertEqual(caught.exception.text, composition)

    def test_text_rewrite_is_once_and_does_not_touch_nonreferences(self) -> None:
        original = "[[F1]] 与 [[F10]]; 参数 10, 沿 [[F7]]"
        self.assertEqual(rewrite_text(original, {"F1": "F7", "F7": "F9"}), "[[F7]] 与 [[F10]]; 参数 10, 沿 [[F9]]")

    def test_mapping_rejects_missing_cycle_and_cross_type(self) -> None:
        index = {"F1": "features", "F2": "features", "M1": "mechanisms"}
        for mapping in ({"F1": "F9"}, {"F1": "F2", "F2": "F1"}, {"F1": "M1"}):
            with self.subTest(mapping=mapping), self.assertRaises(LibraryValidationError):
                resolve_mapping(index, mapping)

    def test_reference_types_are_from_index(self) -> None:
        submission = ExplorationSubmission(
            features=(Feature(id="observation", description="边缘", mechanism_refs=(MechanismRef(id="F1"),)),),
            relations=(),
            mechanisms=(Mechanism(id="F1", name="方法", method="输出遮罩"),),
            sketches=(Sketch(id="plan", element_id="E1", name="方案", default={"observation": ("F1",)}, composition="沿 [[observation]] 裁切"),),
        )
        validate_exploration(submission, target())
        library, _, _ = collect_explorations(target(), {"a": submission})
        self.assertEqual(library.sketches[0].default, {"F1": ("M1",)})

    def test_mechanism_cannot_bind_consumer(self) -> None:
        method = report().mechanisms[0].model_copy(update={"method": "实现 [[F1]]"})
        with self.assertRaisesRegex(LibraryValidationError, "消费者"):
            validate_exploration(report().model_copy(update={"mechanisms": (method,)}), target())

    def test_merge_features_keeps_each_sketch_plan_and_candidate_union(self) -> None:
        library, questions, _ = collect_explorations(target(), {"a": report(), "b": report()})
        original = library.model_dump_json()
        merges = proposal("features", ("F1", "F2"), "F1")
        merged = apply_merges(library, merges, "E1")
        self.assertEqual(len(merged.features), 1)
        self.assertEqual(merged.elements[0].feature_ids, ("F1",))
        self.assertEqual({ref.id for ref in merged.features[0].mechanism_refs}, {"M1", "M2"})
        self.assertEqual(merged.sketches[1].default, {"F1": ("M2",)})
        self.assertEqual(merged.sketches[1].composition, "沿 [[F1]] 构建遮罩")
        self.assertEqual(library.model_dump_json(), original)
        self.assertEqual(rewrite_issues(questions, merge_mapping(library, merges))[1].refs, ("F1",))

    def test_mechanism_merge_updates_all_candidates_and_selections(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        merged = apply_merges(library, proposal("mechanisms", ("M1", "M2"), "M1"), "E1")
        self.assertEqual(len(merged.mechanisms), 1)
        self.assertEqual(merged.features[1].mechanism_refs[0].id, "M1")
        self.assertEqual(merged.sketches[1].default, {"F2": ("M1",)})

    def test_default_collision_rejects_whole_merge_without_mutation(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        sketch = library.sketches[0].model_copy(update={"default": {"F1": ("M1",), "F2": ("M2",)}})
        library = library.model_copy(update={"sketches": (sketch,)})
        before = library.model_dump_json()
        with self.assertRaisesRegex(LibraryValidationError, "撞键"):
            apply_merges(library, proposal("features", ("F1", "F2"), "F1"), "E1")
        self.assertEqual(library.model_dump_json(), before)

    def test_relation_endpoint_collapse_is_rejected(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        relation = Relation(
            id="R1", participants=(Participant(ref="F1"), Participant(ref="F2")), description="两处轮廓相邻", mechanism_refs=(MechanismRef(id="M1"),)
        )
        library = library.model_copy(update={"relations": (relation,)})
        with self.assertRaisesRegex(LibraryValidationError, "端点塌缩"):
            apply_merges(library, proposal("features", ("F1", "F2"), "F1"), "E1")

    def test_similar_relations_with_different_roles_are_not_merged(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        relation = Relation(
            id="R1",
            participants=(Participant(ref="F1", role="前"), Participant(ref="F2", role="后")),
            description="遮挡",
            mechanism_refs=(MechanismRef(id="M1"),),
        )
        other = relation.model_copy(update={"id": "R2", "participants": (Participant(ref="F1", role="后"), Participant(ref="F2", role="前"))})
        library = library.model_copy(update={"relations": (relation, other)})
        with self.assertRaisesRegex(LibraryValidationError, "作用不同"):
            apply_merges(library, proposal("relations", ("R1", "R2"), "R1"), "E1")

    def test_overlapping_merge_groups_rejected(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report(), "c": report()})
        merges = MergeProposal(
            groups=(MergeGroup(kind="features", ids=("F1", "F2"), keep="F1"), MergeGroup(kind="features", ids=("F2", "F3"), keep="F2"))
        )
        with self.assertRaisesRegex(LibraryValidationError, "重叠"):
            apply_merges(library, merges, "E1")

    def test_sketch_edits_and_new_objects_not_in_merge_schema(self) -> None:
        with self.assertRaises(ValidationError):
            MergeProposal.model_validate({"groups": [], "sketches": []})
        with self.assertRaises(ValidationError):
            MergeGroup.model_validate({"kind": "sketches", "ids": ["S1", "S2"], "keep": "S1"})

    def test_duplicate_ids_across_libraries_rejected(self) -> None:
        submission = report().model_copy(
            update={"features": (report().features[0].model_copy(update={"id": "M1"}),), "sketches": (), "open_questions": ()}
        )
        with self.assertRaisesRegex(LibraryValidationError, "ID 重复"):
            validate_exploration(submission, target())

    def test_invalid_selection_is_not_allowed_by_existence_alone(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        sketch = library.sketches[0].model_copy(update={"default": {"F1": ("M2",)}})
        with self.assertRaisesRegex(LibraryValidationError, "候选机制"):
            validate_libraries(library.model_copy(update={"sketches": (sketch,)}), "E1")

    def test_markers_preserve_whitespace_and_case_identity(self) -> None:
        self.assertEqual(parse_references("[[F1]]", {"F1": "features"}, field="body"), ("F1",))
        with self.assertRaisesRegex(LibraryValidationError, "精确匹配"):
            parse_references("[[f1]]", {"F1": "features"}, field="body")

    def test_alternative_namespace_preserves_complete_organization(self) -> None:
        original = report()
        feature = original.features[0].model_copy(update={"mechanism_refs": (MechanismRef(id="M1"), MechanismRef(id="M2"))})
        mechanism = Mechanism(id="M2", name="高斯", method="缩放坐标后做高斯衰减")
        alternative = Alternative(
            choices={"F1": ("M2",)}, reason="[[F1]] 的边缘未知", conditions=("沿 [[F1]] 裁切",), composition="以 [[M2]] 重建 [[F1]]"
        )
        sketch = original.sketches[0].model_copy(update={"alternatives": (alternative,)})
        submission = original.model_copy(update={"features": (feature,), "mechanisms": (*original.mechanisms, mechanism), "sketches": (sketch,)})
        library, _, _ = collect_explorations(target(), {"a": submission, "b": submission})
        second = library.sketches[1].alternatives[0]
        self.assertEqual(second.choices, {"F2": ("M4",)})
        self.assertEqual(second.composition, "以 [[M4]] 重建 [[F2]]")
        self.assertEqual(second.reason, "[[F2]] 的边缘未知")
        self.assertEqual(second.conditions, ("沿 [[F2]] 裁切",))

    def test_alternative_cannot_inherit_organization_of_removed_mechanism(self) -> None:
        original = report()
        feature = original.features[0].model_copy(update={"mechanism_refs": (MechanismRef(id="M1"), MechanismRef(id="M2"))})
        mechanism = Mechanism(id="M2", name="高斯", method="缩放坐标后做高斯衰减")
        alternative = Alternative(choices={"F1": ("M2",)}, reason="边缘未知")
        sketch = original.sketches[0].model_copy(update={"composition": "使用 [[M1]] 构造 [[F1]]", "alternatives": (alternative,)})
        submission = original.model_copy(update={"features": (feature,), "mechanisms": (*original.mechanisms, mechanism), "sketches": (sketch,)})
        with self.assertRaisesRegex(LibraryValidationError, "未采用机制"):
            validate_exploration(submission, target())

    def test_joint_mechanism_collapse_is_rejected(self) -> None:
        library, _, _ = collect_explorations(target(), {"a": report(), "b": report()})
        feature = library.features[0].model_copy(update={"mechanism_refs": (MechanismRef(id="M1"), MechanismRef(id="M2"))})
        sketch = library.sketches[0].model_copy(update={"default": {"F1": ("M1", "M2")}})
        library = library.model_copy(update={"features": (feature, library.features[1]), "sketches": (sketch,)})
        with self.assertRaisesRegex(LibraryValidationError, "共同采用的机制合并后塌缩"):
            apply_merges(library, proposal("mechanisms", ("M1", "M2"), "M1"), "E1")

    def test_existing_sketch_missing_own_observation_requires_gap(self) -> None:
        original = report()
        other = original.features[0].model_copy(update={"id": "F2", "description": "颜色"})
        submission = original.model_copy(update={"features": (*original.features, other)})
        with self.assertRaisesRegex(LibraryValidationError, "未覆盖本来源观察目标"):
            validate_exploration(submission, target())
        for refs in (("F2",), ("S1",), ()):
            with self.subTest(refs=refs):
                recorded = submission.model_copy(update={"gaps": (Issue(refs=refs, description="本方案尚未实现颜色目标"),)})
                validate_exploration(recorded, target())
        unrelated = submission.model_copy(update={"gaps": (Issue(refs=("F1",), description="轮廓参数未定"),)})
        with self.assertRaisesRegex(LibraryValidationError, "未覆盖本来源观察目标"):
            validate_exploration(unrelated, target())

    def test_existing_sketch_missing_own_relation_requires_gap(self) -> None:
        original = report()
        relation = Relation(
            id="R1",
            participants=(Participant(ref="E1"), Participant(ref="F1")),
            description="主体由轮廓限定",
            mechanism_refs=(MechanismRef(id="M1"),),
        )
        submission = original.model_copy(update={"relations": (relation,)})
        with self.assertRaisesRegex(LibraryValidationError, "未覆盖本来源观察目标"):
            validate_exploration(submission, target())
        validate_exploration(submission.model_copy(update={"gaps": (Issue(refs=("R1",), description="方案中关系尚未组织"),)}), target())

    def test_collected_sketch_does_not_have_to_cover_other_sources(self) -> None:
        library, _, gaps = collect_explorations(target(), {"a": report(), "b": report()})
        self.assertEqual(set(library.sketches[0].default), {"F1"})
        self.assertEqual(set(library.sketches[1].default), {"F2"})
        self.assertEqual(gaps, ())
        validate_libraries(library, "E1")


if __name__ == "__main__":
    unittest.main()
