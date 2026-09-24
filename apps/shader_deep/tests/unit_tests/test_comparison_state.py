"""显式比较范围、实际呈现和必要复核之间的行为边界."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast

from shader_deep.analysis.integration import IntegrationDecision, MergeDecision
from shader_deep.analysis.library import LibraryStore
from shader_deep.analysis.materials import ComparisonManager, Document
from tests.unit_tests.test_integration_materials import SnapshotStore
from tests.unit_tests.test_possibility_library import outline, report


class ComparisonStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SnapshotStore()
        self.store.add("F1")
        self.store.add("F2")
        self.store.add("S2")
        self.manager = ComparisonManager()

    def select(self, *identifiers: str, present: bool = True) -> None:
        entries = self.store.material_entries()
        versions = self.store.material_versions(list(identifiers))
        self.manager.append({identifier: entries[identifier] for identifier in identifiers}, versions, library_revision=self.store.revision)
        if present:
            self.manager.mark_presented(versions)

    def accept(self, decision: IntegrationDecision) -> None:
        self.manager.validate_decision(self.store, decision)
        self.manager.accept(self.store, decision, {"status": "accepted"})

    def test_preserve_only_closes_targets_not_support_or_candidates(self) -> None:
        self.manager.start(self.store, "比较外观", ["F1", "F2"])
        self.select("F1", "F2", "S2", "F1:C")
        self.accept(IntegrationDecision(preserve=True))
        statuses = self.manager.status_summary(self.store)
        self.assertEqual(statuses["F1"], "compared_preserved")
        self.assertEqual(statuses["F2"], "compared_preserved")
        self.assertEqual(statuses["S2"], "unexpanded_preserved")
        self.assertEqual(statuses["F1:C"], "unexpanded_preserved")
        self.assertFalse(self.manager.pending)

    def test_appending_deduplicates_without_silently_expanding_scope(self) -> None:
        group = self.manager.start(self.store, "比较外观", ["F1"])
        self.select("F1")
        version = group.version
        self.select("F1")
        self.assertEqual(group.version, version)
        self.select("S2", present=False)
        self.assertEqual(set(group.entries), {"F1", "S2"})
        self.assertEqual(group.target_ids, ("F1",))
        self.assertEqual(set(group.presented_versions), {"F1"})
        with self.assertRaisesRegex(ValueError, "active comparison"):
            self.manager.start(self.store, "新问题", ["F2"])

    def test_unpresented_targets_cannot_be_preserved_but_can_be_deferred(self) -> None:
        self.manager.start(self.store, "比较外观", ["F1", "F2"])
        self.select("F1", "F2", present=False)
        with self.assertRaisesRegex(ValueError, "present comparison target"):
            self.accept(IntegrationDecision(preserve=True))
        self.accept(IntegrationDecision(deferred_work=("必要正文无法容纳",)))
        self.assertEqual(len(self.manager.pending), 1)
        self.assertEqual(self.manager.deferred[0]["ids"], ["F1", "F2"])

    def test_scope_and_untouched_targets_are_enforced(self) -> None:
        self.manager.start(self.store, "比较外观", ["F1", "F2"])
        self.select("F1", "F2", "S2")
        outside = IntegrationDecision(merges=(MergeDecision(kind="feature", members=("F1", "S2")),), preserve=True)
        with self.assertRaisesRegex(ValueError, "outside comparison targets"):
            self.accept(outside)
        with self.assertRaisesRegex(ValueError, "Unaddressed comparison targets"):
            self.accept(IntegrationDecision(review_ids=("F2",)))
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            self.accept(IntegrationDecision(preserve=True, deferred_work=("尚未比较",)))
        self.manager.extend_targets(self.store, ["S2"])
        self.accept(outside)
        self.assertEqual(self.manager.status_summary(self.store)["F2"], "compared_preserved")

    def test_explicit_review_retains_exact_candidates_and_work_identity(self) -> None:
        self.manager.start(self.store, "候选比较", ["F1:C"])
        self.select("F1:C")
        self.accept(IntegrationDecision(preserve=True, review_ids=("F1:C",)))
        work = self.manager.pending[0]
        self.assertEqual(work["target_ids"], ["F1:C"])
        self.manager.start(self.store, "另一个比较", ["F1:C"])
        self.select("F1:C")
        self.accept(IntegrationDecision(preserve=True))
        self.assertEqual(self.manager.pending[0]["work_id"], work["work_id"])
        self.assertEqual(self.manager.status_summary(self.store)["F1:C"], "pending")
        self.manager.start(self.store, "", [], work_id=str(work["work_id"]))
        self.select("F1:C")
        self.accept(IntegrationDecision(preserve=True))
        self.assertFalse(self.manager.pending)

    def test_substantive_support_change_creates_one_review_for_original_scope(self) -> None:
        self.manager.start(self.store, "比较外观", ["F1"])
        self.select("F1", "S2")
        self.accept(IntegrationDecision(preserve=True))
        cast("Document", self.store.entries["S2"]["content"])["appearance"] = "适用条件变化"
        self.manager.refresh(self.store)
        self.manager.refresh(self.store)
        self.assertEqual(len(self.manager.pending), 1)
        self.assertEqual(self.manager.pending[0]["target_ids"], ["F1"])
        self.assertEqual(self.manager.pending[0]["reason"], "substantive_material_change")

    def test_pure_alias_rewrite_does_not_create_review(self) -> None:
        self.manager.start(self.store, "候选比较", ["F1:C"])
        self.select("F1:C")
        self.accept(IntegrationDecision(preserve=True))
        self.store.aliases["F1:C"] = "renamed"
        candidate = self.store.entries.pop("F1:C")
        candidate["handle"] = "renamed"
        cast("Document", candidate["content"])["id"] = "renamed"
        self.store.entries["renamed"] = candidate
        self.manager.refresh(self.store)
        self.assertFalse(self.manager.pending)

    def test_refresh_keeps_scope_and_only_invalidates_changed_presentation(self) -> None:
        group = self.manager.start(self.store, "比较外观", ["F1"])
        self.select("F1", "S2")
        version = group.version
        cast("Document", self.store.entries["S2"]["content"])["appearance"] = "不同外观"
        with self.assertRaisesRegex(ValueError, "materials changed"):
            self.accept(IntegrationDecision(preserve=True))
        self.manager.refresh(self.store)
        self.assertGreater(group.version, version)
        self.assertEqual(group.target_ids, ("F1",))
        self.assertEqual(set(group.presented_versions), {"F1"})
        self.assertEqual(cast("Document", group.entries["S2"]["content"])["appearance"], "不同外观")

    def test_deferred_work_keeps_modifications_but_does_not_close_scope(self) -> None:
        self.manager.start(self.store, "比较外观", ["F1", "F2", "S2"])
        self.select("F1", "F2")
        self.accept(IntegrationDecision(merges=(MergeDecision(kind="feature", members=("F1", "F2")),), deferred_work=("S2 尚需补充材料",)))
        self.assertEqual(self.manager.pending[0]["status"], "deferred")
        self.assertEqual(self.manager.pending[0]["modified_ids"], ["F1", "F2"])
        self.assertEqual(self.manager.status_summary(self.store)["F1"], "deferred")

    def test_snapshot_keeps_independent_selection_versions_and_gaps(self) -> None:
        group = self.manager.start(self.store, "比较外观", ["F1", "F2"])
        self.select("F1")
        group.gaps.append({"ids": ["F2"], "reason": "input_budget_exceeded"})
        snapshot = self.manager.snapshot()
        active = cast("Document", snapshot["active"])
        self.assertEqual(active["target_ids"], ["F1", "F2"])
        self.assertEqual(active["presented_versions"], self.store.material_versions(["F1"]))
        cast("list[object]", active["target_ids"]).clear()
        self.assertEqual(group.target_ids, ("F1", "F2"))
        self.manager.defer()
        self.assertEqual(self.manager.deferred[0]["gaps"], [{"ids": ["F2"], "reason": "input_budget_exceeded"}])

    def test_new_local_review_uses_canonical_receipt_and_preserves_candidate_granularity(self) -> None:
        self.manager.start(self.store, "补充外观", ["F1"])
        self.select("F1")
        decision = IntegrationDecision(preserve=True, additions=report(), review_ids=("C",))
        self.manager.validate_decision(self.store, decision)
        self.store.add("main::F")
        self.manager.accept(self.store, decision, {"status": "accepted", "review_ids": ["main::F:C"]})
        self.assertEqual(self.manager.pending[0]["target_ids"], ["main::F:C"])

    def test_summary_omits_body_history_and_planning_does_not_advance_state(self) -> None:
        group = self.manager.start(self.store, "比较外观", ["F1"])
        self.select("F1")
        decision = IntegrationDecision(preserve=True)
        planned = self.manager.planned_progress(decision)
        self.assertEqual(planned["presented_versions"], group.presented_versions)
        self.assertEqual(self.manager.pending[0]["status"], "comparing")
        self.accept(decision)
        summary = self.manager.summary()
        works = cast("list[Document]", summary["works"])
        self.assertEqual(works[0]["status"], "compared_preserved")
        self.assertFalse(set(works[0]) & {"decision", "receipt", "presented_versions", "selection"})
        self.assertEqual(self.manager.summary(max_records=0)["omitted_work_count"], 1)
        self.assertTrue(self.manager.snapshot()["judgment_baselines"])

    def test_resumed_deferred_work_can_publish_a_new_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LibraryStore(outline(), Path(temporary))
            store.add_report("one", report())
            group = self.manager.start(store, "比较外观", ["one::F"])
            receipts: list[Document] = []
            for decision in (IntegrationDecision(deferred_work=("需后续复核",)), IntegrationDecision(preserve=True)):
                versions = store.material_versions(["one::F"])
                self.manager.append({"one::F": store.material_entries()["one::F"]}, versions, library_revision=store.revision)
                self.manager.mark_presented(versions)
                store.present_materials(versions)
                self.manager.validate_decision(store, decision)
                receipt = store.apply_integration(
                    decision,
                    submission_id=f"{group.id}-v{group.version}",
                    package_id=group.id,
                    editable_ids=list(group.target_ids),
                    material_versions=group.presented_versions,
                )
                self.manager.accept(store, decision, receipt)
                receipts.append(receipt)
                if decision.deferred_work:
                    previous_version = group.version
                    group = self.manager.start(store, "", [], work_id=group.id)
                    self.assertGreater(group.version, previous_version)
            self.assertNotEqual(receipts[0]["submission_id"], receipts[1]["submission_id"])
            self.assertEqual(receipts[1]["revision"], cast("int", receipts[0]["revision"]) + 1)
            self.assertFalse(self.manager.pending)

    def test_summary_pages_pending_work_without_dropping_completion_obligations(self) -> None:
        for index in range(35):
            self.manager.start(self.store, f"需要复核的范围 {index}", ["F1"])
            self.manager.defer("仍需材料")
        self.manager.start(self.store, "已完成范围", ["F2"])
        self.select("F2")
        self.accept(IntegrationDecision(preserve=True))
        summary = self.manager.summary()
        records = cast("list[Document]", summary["works"])
        self.assertLessEqual(len(records), 30)
        self.assertTrue(all(record["status"] == "deferred" for record in records))
        self.assertEqual(summary["pending_work_count"], 35)
        self.assertEqual(summary["omitted_work_count"], 36 - len(records))
        next_page = self.manager.list_work(cursor=cast("str", summary["next_cursor"]))
        all_records = [*records, *cast("list[Document]", next_page["works"])]
        self.assertTrue({work["work_id"] for work in self.manager.pending}.issubset({record["work_id"] for record in all_records}))
