"""完整材料与批量决定的原子发布、版本及候选保留边界."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Lock
from unittest.mock import patch

from langchain.tools import tool

from shader_deep.domain.library.decisions import IntegrationDecision, MergeDecision, OutlineIssueDecision
from shader_deep.domain.library.models import Candidate, ElementScope, Requirement
from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
from shader_deep.runtime.submissions.handler import SubmissionHandler
from tests.unit_tests.domain.test_possibility_library import outline, report


class IntegrationLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = LibraryStore(outline(), Path(self.temporary.name))
        self.store.add_report("one", report())
        self.store.add_report("two", report("细线"))
        self.versions = self.store.material_versions(list(self.store.material_entries()))
        self.store.present_materials(self.versions)

    def apply(self, decision: IntegrationDecision, **overrides: object) -> dict[str, object]:
        arguments = {
            "submission_id": "submit-1",
            "package_id": "package-1",
            "editable_ids": list(self.versions),
            "material_versions": self.versions,
            **overrides,
        }
        return self.store.apply_integration(decision, **arguments)

    def test_multiple_merges_are_one_revision_and_preserve_unmerged_candidates(self) -> None:
        revision = self.store.revision
        decision = IntegrationDecision(
            merges=(
                MergeDecision(kind="candidate", members=("two::C", "one::C")),
                MergeDecision(kind="feature", members=("two::F", "one::F")),
            )
        )
        receipt = self.apply(decision)
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(receipt["revision"], revision + 1)
        self.assertEqual(len(self.store.library.feature_library), 1)
        self.assertEqual({item.id for item in self.store.library.feature_library[0].candidates}, {"one::C", "one::U", "two::U"})
        self.assertEqual(self.store.library.sketch_library[1].feature_refs[0].candidate_refs[0].candidate_id, "one::C")
        snapshot = json.loads((self.store.directory / f"version-{self.store.revision:04d}.json").read_text())
        self.assertEqual(snapshot["integration_receipts"]["submit-1"]["receipt"], receipt)
        self.assertEqual(snapshot["integration_progress"]["package-1"]["status"], "accepted")

    def test_late_invalid_operation_rolls_back_earlier_merge(self) -> None:
        library, revision = self.store.library, self.store.revision
        decision = IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),), unresolved={"missing": ("无法覆盖",)})
        with self.assertRaisesRegex(ValueError, "/unresolved"):
            self.apply(decision)
        self.assertEqual(self.store.library, library)
        self.assertEqual(self.store.revision, revision)
        self.assertFalse(self.store.aliases)
        self.assertIsNone(self.store.integration_receipt("submit-1"))
        self.assertEqual(len(list(self.store.directory.glob("version-*.json"))), revision)

    def test_publish_failure_keeps_state_receipts_and_sources_unchanged(self) -> None:
        library, revision = self.store.library, self.store.revision
        decision = IntegrationDecision(additions=report("新机制"))
        with patch.object(Path, "replace", side_effect=OSError("disk failure")), self.assertRaisesRegex(OSError, "disk failure"):
            self.apply(decision)
        self.assertEqual(self.store.library, library)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(self.store.report_ids, ("one", "two"))
        self.assertFalse((self.store.directory / "source-main-1.json").exists())
        self.assertIsNone(self.store.integration_receipt("submit-1"))

    def test_submission_replay_returns_original_receipt_and_rejects_different_content(self) -> None:
        decision = IntegrationDecision(additions=report("新增但不重复执行"))
        receipt = self.apply(decision)
        revision = self.store.revision
        self.assertEqual(self.apply(decision), receipt)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(len(self.store.library.feature_library), 3)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.apply(IntegrationDecision(preserve=True))

    def test_report_replay_returns_original_handles_after_they_were_merged(self) -> None:
        original = self.store.add_report("two", report("细线"))
        self.apply(IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),)))
        self.assertEqual(self.store.add_report("two", report("细线")), original)

    def test_scope_and_actual_presentation_are_required(self) -> None:
        decision = IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),))
        with self.assertRaisesRegex(ValueError, "/merges/0/members/1"):
            self.apply(decision, editable_ids=["one::F"])
        self.store.presented.clear()
        with self.assertRaisesRegex(ValueError, "not presented"):
            self.apply(IntegrationDecision(preserve=True))
        self.store.present_materials(self.versions)
        self.assertEqual(self.apply(IntegrationDecision(preserve=True))["status"], "accepted")

    def test_readonly_dependency_can_be_mechanically_rewritten(self) -> None:
        self.apply(
            IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),)),
            editable_ids=["one::F", "two::F"],
        )
        self.assertEqual(self.store.library.sketch_library[1].feature_refs[0].feature_id, "one::F")

    def test_changed_related_version_rejects_while_unrelated_revision_is_allowed(self) -> None:
        handles = self.store.material_closure(["one::F"])
        versions = self.store.material_versions(handles)
        self.store.add_report("three", report("无关的新来源"))
        self.apply(IntegrationDecision(preserve=True), material_versions=versions, editable_ids=handles)
        self.store.merge("candidate", ["one::C", "one::U"], "one::C")
        with self.assertRaisesRegex(ValueError, "Material"):
            self.apply(IntegrationDecision(preserve=True), submission_id="stale", material_versions=versions, editable_ids=handles)

    def test_preserve_does_not_require_unrelated_library_to_be_read(self) -> None:
        self.store.presented.clear()
        handles = self.store.material_closure(["one::S"])
        versions = self.store.material_versions(handles)
        self.store.present_materials(versions)
        self.assertIn("two::U", self.store.unread_handles)
        self.assertEqual(self.apply(IntegrationDecision(preserve=True), material_versions=versions, editable_ids=handles)["status"], "accepted")

    def test_reference_update_preserves_other_sketches_and_all_candidates(self) -> None:
        original = self.store.library
        sketch = replace(original.sketch_library[0], unresolved=("需要渲染对照",))
        self.apply(IntegrationDecision(reference_updates=(sketch,)))
        self.assertEqual(self.store.library.sketch_library[1], original.sketch_library[1])
        self.assertEqual(self.store.library.feature_library, original.feature_library)
        self.assertEqual(self.store.library.sketch_library[0].unresolved, ("需要渲染对照",))

    def test_merge_and_reference_update_share_the_same_canonical_mapping(self) -> None:
        first, second = self.store.library.sketch_library
        combined = replace(first, id="combined", feature_refs=(*first.feature_refs, *second.feature_refs))
        self.store.add_sketch(combined)
        self.versions = self.store.material_versions(list(self.store.material_entries()))
        self.store.present_materials(self.versions)
        self.apply(
            IntegrationDecision(
                merges=(
                    MergeDecision(kind="feature", members=("one::F", "two::F")),
                    MergeDecision(kind="candidate", members=("one::C", "two::C")),
                ),
                reference_updates=(replace(combined, unresolved=("仍待渲染",)),),
            )
        )
        updated = next(sketch for sketch in self.store.library.sketch_library if sketch.id == "combined")
        self.assertEqual(len(updated.feature_refs), 1)
        self.assertEqual(len(updated.feature_refs[0].candidate_refs), 1)
        self.assertEqual(updated.unresolved, ("仍待渲染",))

    def test_issue_decision_and_progress_share_library_snapshot(self) -> None:
        decision = IntegrationDecision(
            outline_issue_decisions=(OutlineIssueDecision(issue_id="issue-1", disposition="deferred", reason="待修订元素"),)
        )
        receipt = self.apply(decision, progress={"processed_handles": list(self.versions)})
        snapshot = json.loads((self.store.directory / f"version-{self.store.revision:04d}.json").read_text())
        self.assertEqual(snapshot["integration_receipts"]["submit-1"]["receipt"]["outline_issue_decisions"], receipt["outline_issue_decisions"])
        self.assertEqual(snapshot["integration_progress"]["package-1"]["processed_handles"], list(self.versions))
        self.assertEqual(snapshot["outline"], json.loads(json.dumps(asdict(outline()))))

    def test_materials_have_unique_candidate_bodies_and_candidate_membership_hash(self) -> None:
        entries = self.store.material_entries()
        self.assertNotIn("candidates", entries["one::F"]["content"])
        self.assertEqual(entries["one::F"]["candidate_ids"], ["one::C", "one::U"])
        self.assertEqual(entries["one::U"]["owner"], "one::F")
        versions = self.store.material_versions(["one::F"])
        self.store.merge("candidate", ["one::C", "one::U"], "one::C")
        self.assertNotEqual(self.store.material_versions(["one::F"]), versions)
        with self.assertRaisesRegex(ValueError, "versions changed"):
            self.store.present_materials(versions)

    def test_parallel_report_imports_do_not_lose_other_task_content(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda identifier: self.store.add_report(identifier, report(identifier)), ["three", "four"]))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(set(self.store.report_ids), {"one", "two", "three", "four"})
        self.assertEqual(len(self.store.library.feature_library), 4)

    def test_empty_decision_requires_explicit_preserve_and_overlap_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "/preserve"):
            self.apply(IntegrationDecision())
        decision = IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),) * 2)
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            self.apply(decision)

    def test_review_scope_resolves_merged_and_new_local_identifiers(self) -> None:
        receipt = self.apply(
            IntegrationDecision(
                merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),),
                additions=report("新增比较对象"),
                review_ids=("two::F", "F"),
                deferred_work=("需要与下一包的空间组织共同比较",),
            )
        )
        self.assertEqual(receipt["review_ids"], ["one::F", "main-1::F"])
        self.assertEqual(receipt["deferred_work"], ["需要与下一包的空间组织共同比较"])
        self.assertEqual(self.store.integration_receipt("submit-1"), receipt)

    def test_review_can_preserve_an_element_as_an_independent_work_target(self) -> None:
        original = self.store.library
        receipt = self.apply(IntegrationDecision(preserve=True, review_ids=("E",)), editable_ids=["E"], material_versions={"E": self.versions["E"]})
        self.assertEqual(receipt["review_ids"], ["E"])
        self.assertEqual(self.store.library, original)
        snapshot = json.loads((self.store.directory / f"version-{self.store.revision:04d}.json").read_text())
        self.assertEqual(snapshot["integration_receipts"]["submit-1"]["receipt"]["review_ids"], ["E"])

    def test_invalid_review_scope_prevents_entire_publication(self) -> None:
        original, revision = self.store.library, self.store.revision
        with self.assertRaisesRegex(ValueError, "/review_ids/0"):
            self.apply(IntegrationDecision(additions=report(), review_ids=("missing",)))
        self.assertEqual(self.store.library, original)
        self.assertEqual(self.store.revision, revision)
        self.assertIsNone(self.store.integration_receipt("submit-1"))

    def test_review_and_deferred_work_are_valid_without_library_changes(self) -> None:
        original = self.store.library
        receipt = self.apply(IntegrationDecision(review_ids=("one::F",), deferred_work=("当前范围尚未完成比较",)))
        self.assertEqual(self.store.library, original)
        self.assertEqual(receipt["review_ids"], ["one::F"])
        with self.assertRaises(ValueError):
            IntegrationDecision(deferred_work=(" ",))

    def test_reference_update_business_error_can_be_repaired_at_reported_path(self) -> None:
        @tool(args_schema=IntegrationDecision)
        def submit_integration(**arguments: object) -> str:
            """提交测试决定, 复用真实工具契约与库发布路径."""
            return json.dumps(self.apply(IntegrationDecision.model_validate(arguments)))

        handler = SubmissionHandler(
            "integration",
            submit_integration,
            lambda arguments: json.dumps(self.apply(IntegrationDecision.model_validate(arguments))),
            lambda: None,
            Lock(),
        )
        # 只修改整库中第二张草图, 验证库位置 1 被映射回参数位置 0.
        sketch = self.store.library.sketch_library[1]
        reference = replace(sketch.feature_refs[0], applies_to=(ElementScope(element_id="E", group_id="missing"),))
        decision = IntegrationDecision(reference_updates=(replace(sketch, feature_refs=(reference,)),))
        rejected = json.loads(handler.handle("submit_integration", decision.model_dump(mode="json")).content)
        path = rejected["errors"][0]["path"]
        self.assertEqual(path, "/reference_updates/0/feature_refs/0/applies_to/0/group_id")
        revision = self.store.revision
        repaired = handler.handle(
            "repair_analysis_submission",
            {
                "draft_id": rejected["draft_id"],
                "expected_revision": rejected["revision"],
                "changes": [{"op": "remove", "path": path}],
            },
        )
        self.assertEqual(json.loads(repaired.content)["status"], "accepted")
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(self.store.library.sketch_library[1].feature_refs[0].applies_to, (ElementScope(element_id="E"),))

    def test_addition_business_error_maps_library_offset_to_repairable_local_index(self) -> None:
        @tool(args_schema=IntegrationDecision)
        def submit_integration(**arguments: object) -> str:
            """提交测试增量, 复用真实工具契约与库发布路径."""
            return json.dumps(self.apply(IntegrationDecision.model_validate(arguments)))

        handler = SubmissionHandler(
            "integration",
            submit_integration,
            lambda arguments: json.dumps(self.apply(IntegrationDecision.model_validate(arguments))),
            lambda: None,
            Lock(),
        )
        addition = report("新机制")
        feature = replace(addition.feature_library[0], element_ids=("missing",))
        decision = IntegrationDecision(additions=replace(addition, feature_library=(feature,)))
        rejected = json.loads(handler.handle("submit_integration", decision.model_dump(mode="json")).content)
        path = rejected["errors"][0]["path"]
        self.assertEqual(path, "/additions/feature_library/0/element_ids/0")
        revision = self.store.revision
        repaired = handler.handle(
            "repair_analysis_submission",
            {
                "draft_id": rejected["draft_id"],
                "expected_revision": rejected["revision"],
                "changes": [{"op": "set", "path": path, "value": "E"}],
            },
        )
        self.assertEqual(json.loads(repaired.content)["status"], "accepted")
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(len(self.store.library.feature_library), 3)
        self.assertEqual(self.store.library.feature_library[-1].element_ids, ("E",))

    def test_addition_sketch_choice_error_maps_to_repairable_local_reference(self) -> None:
        @tool(args_schema=IntegrationDecision)
        def submit_integration(**arguments: object) -> str:
            """提交测试增量, 保留子报告候选身份数组契约."""
            return json.dumps(self.apply(IntegrationDecision.model_validate(arguments)))

        handler = SubmissionHandler(
            "integration",
            submit_integration,
            lambda arguments: json.dumps(self.apply(IntegrationDecision.model_validate(arguments))),
            lambda: None,
            Lock(),
        )
        addition = report("候选引用待修复")
        sketch = addition.sketch_library[0]
        reference = replace(sketch.feature_refs[0], candidate_ids=("missing",))
        decision = IntegrationDecision(additions=replace(addition, sketch_library=(replace(sketch, feature_refs=(reference,)),)))
        rejected = json.loads(handler.handle("submit_integration", decision.model_dump(mode="json")).content)
        path = rejected["errors"][0]["path"]
        self.assertEqual(path, "/additions/sketch_library/0/feature_refs/0")
        repaired = handler.handle(
            "repair_analysis_submission",
            {
                "draft_id": rejected["draft_id"],
                "expected_revision": rejected["revision"],
                "changes": [{"op": "set", "path": path, "value": {"feature_id": "F", "candidate_ids": ["C"]}}],
            },
        )
        self.assertEqual(json.loads(repaired.content)["status"], "accepted")
        self.assertEqual(self.store.library.sketch_library[-1].feature_refs[0].candidate_refs[0].candidate_id, "main-1::C")

    def test_owner_merge_needs_owner_bodies_but_not_candidate_bodies(self) -> None:
        owners = ["one::F", "two::F"]
        self.store.presented.clear()
        versions = self.store.material_versions(owners)
        self.store.present_materials(versions)
        self.apply(
            IntegrationDecision(merges=(MergeDecision(kind="feature", members=tuple(owners)),)),
            editable_ids=owners,
            material_versions=versions,
        )
        self.assertEqual({item.id for item in self.store.library.feature_library[0].candidates}, {"one::C", "one::U", "two::C", "two::U"})

    def test_candidate_merge_needs_owner_body_and_direct_prerequisites_only(self) -> None:
        original = report().feature_library[0]
        requirement = Requirement(kind="feature", id="P", candidate_ids=("PC",))
        first = replace(original, candidates=tuple(replace(candidate, requires=(requirement,)) for candidate in original.candidates))
        prerequisite = replace(
            original,
            id="P",
            candidates=(
                Candidate(
                    id="PC", mechanism="直接前提", reasoning="用于比较", requires=(Requirement(kind="feature", id="Q", candidate_ids=("QC",)),)
                ),
            ),
        )
        indirect = replace(original, id="Q", candidates=(Candidate(id="QC", mechanism="间接前提", reasoning="不需要递归展开"),))
        self.store.add_report("chain", replace(report(), sketch_library=(), feature_library=(first, prerequisite, indirect)))
        handles = ["chain::C", "chain::U", "chain::F", "chain::P", "chain::PC"]
        versions = self.store.material_versions(handles)
        self.store.present_materials(versions)
        decision = IntegrationDecision(merges=(MergeDecision(kind="candidate", members=("chain::C", "chain::U")),))
        with self.assertRaisesRegex(ValueError, "chain::F"):
            self.apply(decision, editable_ids=handles, material_versions={key: value for key, value in versions.items() if key != "chain::F"})
        with self.assertRaisesRegex(ValueError, "chain::PC"):
            self.apply(decision, editable_ids=handles, material_versions={key: value for key, value in versions.items() if key != "chain::PC"})
        self.apply(decision, editable_ids=handles, material_versions=versions)
        self.assertNotIn("chain::Q", self.store.presented)
        self.assertEqual(len(next(feature for feature in self.store.library.feature_library if feature.id == "chain::F").candidates), 1)

    def test_reference_change_requires_only_changed_choices_and_owner_bodies(self) -> None:
        sketch = self.store.library.sketch_library[0]
        reference = sketch.feature_refs[0]
        choice = replace(reference.candidate_refs[0], priority="high", priority_reason="优先渲染验证")
        updated = replace(sketch, feature_refs=(replace(reference, candidate_refs=(choice,)),))
        handles = ["one::S", "one::F", "one::C"]
        versions = self.store.material_versions(handles)
        self.store.presented.clear()
        self.store.present_materials(versions)
        self.apply(IntegrationDecision(reference_updates=(updated,)), editable_ids=["one::S"], material_versions=versions)
        self.assertEqual(self.store.library.sketch_library[0].feature_refs[0].candidate_refs[0].priority, "high")
        self.assertEqual(len(self.store.library.feature_library[0].candidates), 2)

    def test_new_sketch_requires_selected_existing_candidate_but_not_other_candidates(self) -> None:
        original = report()
        sketch = original.sketch_library[0]
        reference = replace(sketch.feature_refs[0], feature_id="one::F", candidate_ids=("one::C",))
        addition = replace(original, sketch_library=(replace(sketch, feature_refs=(reference,)),), feature_library=())
        handles = ["one::F", "one::C"]
        versions = self.store.material_versions(handles)
        self.store.presented.clear()
        self.store.present_materials(versions)
        with self.assertRaisesRegex(ValueError, "/additions.*one::C"):
            self.apply(IntegrationDecision(additions=addition), material_versions={"one::F": versions["one::F"]})
        self.apply(IntegrationDecision(additions=addition), material_versions=versions)
        self.assertEqual(self.store.library.sketch_library[-1].feature_refs[0].candidate_refs[0].candidate_id, "one::C")
        self.assertNotIn("one::U", self.store.presented)

    def test_presented_identity_cannot_substitute_for_current_presented_version(self) -> None:
        self.store.update_elements_unresolved({"E": ["新的未解问题"]})
        self.store.presented.add("E")
        versions = self.store.material_versions(["E"])
        with self.assertRaisesRegex(ValueError, "Current material versions were not presented.*E"):
            self.apply(IntegrationDecision(preserve=True), editable_ids=["E"], material_versions=versions)
        self.store.present_materials(versions)
        self.assertEqual(self.apply(IntegrationDecision(preserve=True), editable_ids=["E"], material_versions=versions)["status"], "accepted")

    def test_deferral_does_not_authorize_unread_element_change(self) -> None:
        decision = IntegrationDecision(unresolved={"E": ("仍待验证",)}, deferred_work=("当前比较尚未完成",))
        versions = {key: value for key, value in self.versions.items() if key != "E"}
        before, revision = self.store.library, self.store.revision
        with self.assertRaisesRegex(ValueError, "/unresolved.*E"):
            self.apply(decision, material_versions=versions)
        self.assertEqual(self.store.library, before)
        self.assertEqual(self.store.revision, revision)
        self.apply(decision)
        self.assertEqual(self.store.library.elements[0].unresolved, ("仍待验证",))

    def test_deferred_progress_is_published_with_the_same_atomic_receipt(self) -> None:
        receipt = self.apply(IntegrationDecision(deferred_work=("需要进一步比较",)), progress={"status": "deferred", "target_ids": ["one::F"]})
        snapshot = json.loads((self.store.directory / f"version-{self.store.revision:04d}.json").read_text())
        self.assertEqual(snapshot["integration_progress"]["package-1"]["status"], "deferred")
        self.assertEqual(snapshot["integration_progress"]["package-1"]["target_ids"], ["one::F"])
        self.assertEqual(snapshot["integration_receipts"]["submit-1"]["receipt"], receipt)

    def test_preserve_and_deferred_work_are_mutually_exclusive(self) -> None:
        before, revision = self.store.library, self.store.revision
        with self.assertRaisesRegex(ValueError, "/preserve.*mutually exclusive"):
            self.apply(IntegrationDecision(preserve=True, deferred_work=("尚未完成当前比较",)))
        self.assertEqual(self.store.library, before)
        self.assertEqual(self.store.revision, revision)

    def test_merge_and_reference_repair_allow_temporary_overlap_in_one_transaction(self) -> None:
        first, second = self.store.library.sketch_library
        scoped = replace(second.feature_refs[0], applies_to=(ElementScope(element_id="E"),))
        combined = replace(first, id="combined", feature_refs=(*first.feature_refs, scoped))
        self.store.add_sketch(combined)
        handles = ["one::F", "two::F", "one::C", "two::C", "combined"]
        versions = self.store.material_versions(handles)
        self.store.present_materials(versions)
        repaired = replace(
            combined, feature_refs=(replace(first.feature_refs[0], candidate_refs=(*first.feature_refs[0].candidate_refs, *scoped.candidate_refs)),)
        )
        revision = self.store.revision
        self.apply(
            IntegrationDecision(
                merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),),
                reference_updates=(repaired,),
            ),
            editable_ids=["one::F", "two::F", "combined"],
            material_versions=versions,
        )
        self.assertEqual(self.store.revision, revision + 1)
        updated = self.store.library.sketch_library[-1]
        self.assertEqual(len(updated.feature_refs), 1)
        self.assertEqual({choice.candidate_id for choice in updated.feature_refs[0].candidate_refs}, {"one::C", "two::C"})
        self.assertEqual(len(self.store.library.feature_library[0].candidates), 4)

    def test_merge_derived_overlap_rejects_atomically_and_repairs_actual_members_path(self) -> None:
        self.store.add_report("three", report("第三种解释"))
        first, second = self.store.library.sketch_library[:2]
        scoped = replace(second.feature_refs[0], applies_to=(ElementScope(element_id="E"),))
        combined = replace(first, id="combined", feature_refs=(*first.feature_refs, scoped))
        self.store.add_sketch(combined)
        self.versions = self.store.material_versions(list(self.store.material_entries()))
        self.store.present_materials(self.versions)

        @tool(args_schema=IntegrationDecision)
        def submit_integration(**arguments: object) -> str:
            """提交合并测试, 不放宽原有适用范围校验."""
            return json.dumps(self.apply(IntegrationDecision.model_validate(arguments)))

        handler = SubmissionHandler(
            "integration",
            submit_integration,
            lambda arguments: json.dumps(self.apply(IntegrationDecision.model_validate(arguments))),
            lambda: None,
            Lock(),
        )
        decision = IntegrationDecision(merges=(MergeDecision(kind="feature", members=("one::F", "two::F")),))
        library, revision = self.store.library, self.store.revision
        rejected = json.loads(handler.handle("submit_integration", decision.model_dump(mode="json")).content)
        issue = rejected["errors"][0]
        self.assertEqual(issue["code"], "overlapping_choice")
        self.assertEqual(issue["path"], "/merges/0/members")
        self.assertIn("/sketch_library/3/feature_refs/1", issue["message"])
        self.assertIn("combined", issue["message"])
        self.assertEqual(self.store.library, library)
        self.assertEqual(self.store.revision, revision)
        repaired = handler.handle(
            "repair_analysis_submission",
            {
                "draft_id": rejected["draft_id"],
                "expected_revision": rejected["revision"],
                "changes": [{"op": "set", "path": issue["path"], "value": ["one::F", "three::F"]}],
            },
        )
        self.assertEqual(json.loads(repaired.content)["status"], "accepted")
        self.assertEqual(self.store.revision, revision + 1)
        self.assertEqual(sum(len(feature.candidates) for feature in self.store.library.feature_library), 6)
        self.assertEqual(self.store.library.sketch_library[-1].feature_refs, combined.feature_refs)


if __name__ == "__main__":
    unittest.main()
