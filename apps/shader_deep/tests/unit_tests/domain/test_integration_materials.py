"""完整材料预算、版本刷新和后端队列推进的行为测试."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from shader_deep.compatibility.ondemand.context import build_integration_context
from shader_deep.domain.library.queue import MaterialQueue
from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore
from shader_deep.runtime.usage import request_sizes
from tests.unit_tests.domain.test_possibility_library import outline, report

if TYPE_CHECKING:
    from shader_deep.domain.library.store import Document


class SnapshotStore:
    def __init__(self) -> None:
        self.revision = 1
        self.entries: dict[str, Document] = {}
        self.edges: dict[str, list[str]] = {}
        self.aliases: dict[str, str] = {}

    def add(self, identifier: str, *, size: int = 10, dependencies: tuple[str, ...] = (), element: str = "E") -> None:
        self.entries[identifier] = {
            "handle": identifier,
            "kind": "feature",
            "content": {"id": identifier, "element_ids": [element], "appearance": "完整原文" * size},
            "candidate_ids": [identifier + ":C"],
        }
        self.entries[identifier + ":C"] = {
            "handle": identifier + ":C",
            "kind": "candidate",
            "owner": identifier,
            "content": {"id": identifier + ":C", "mechanism": "机制全文" * size},
        }
        self.edges[identifier] = [identifier + ":C", *dependencies]
        self.edges[identifier + ":C"] = [identifier]
        self.revision += 1

    def resolve(self, identifier: str) -> str:
        return self.aliases.get(identifier, identifier)

    def material_entries(self) -> dict[str, Document]:
        return deepcopy(self.entries)

    def material_closure(self, identifiers: list[str]) -> list[str]:
        queue = list(identifiers)
        result: list[str] = []
        while queue:
            identifier = self.resolve(queue.pop(0))
            if identifier not in result:
                result.append(identifier)
                queue.extend(self.edges.get(identifier, []))
        return result

    def material_versions(self, identifiers: list[str]) -> dict[str, str]:
        return {identifier: hashlib.sha256(json.dumps(self.entries[identifier], sort_keys=True).encode()).hexdigest() for identifier in identifiers}


class MaterialQueueTests(unittest.TestCase):
    def test_material_cost_includes_actual_message_text_escaping(self) -> None:
        store = SnapshotStore()
        for index in range(30):
            identifier = f'quoted"id\\{index}'
            store.add(identifier)
            cast("Document", store.entries[identifier]["content"])["appearance"] = '文本中的"引用"与\\路径\n' * 20
        package = MaterialQueue(1_000_000).next_package(store)
        if package is None:
            self.fail("Expected quote-heavy package")
        empty = build_integration_context("要求", outline(), "data:image/png;base64,A", index={})
        full = build_integration_context("要求", outline(), "data:image/png;base64,A", index=package.business_payload())
        model = FakeListChatModel(responses=[""])
        empty_size = request_sizes(ModelRequest(model=model, messages=[empty]), [])["message_text_chars"]
        full_size = request_sizes(ModelRequest(model=model, messages=[full]), [])["message_text_chars"]
        raw_chars = len(json.dumps(package.business_payload(), ensure_ascii=False, separators=(",", ":")))
        self.assertGreater(full_size - empty_size, raw_chars)
        self.assertEqual(package.material_chars, full_size - empty_size + 2)
        constrained = MaterialQueue(raw_chars)
        smaller = constrained.next_package(store)
        if smaller is None:
            self.fail("Expected a smaller complete package")
        self.assertLess(len(smaller.entries), len(package.entries))
        smaller_message = build_integration_context("要求", outline(), "data:image/png;base64,A", index=smaller.business_payload())
        actual = request_sizes(ModelRequest(model=model, messages=[smaller_message]), [])["message_text_chars"] - empty_size
        self.assertLessEqual(actual, raw_chars)

    def test_full_package_preserves_all_original_text_without_duplicate_bodies(self) -> None:
        store = SnapshotStore()
        store.add("A", dependencies=("B",))
        store.add("B")
        package = MaterialQueue(100_000).next_package(store)
        self.assertIsNotNone(package)
        if package is None:
            self.fail("Expected a complete material package")
        self.assertEqual(set(package.writable_ids), set(store.entries))
        self.assertEqual(len(package.entries), len(store.entries))
        self.assertEqual({entry["handle"]: entry for entry in package.entries}, store.entries)

    def test_multi_package_read_only_dependency_and_changed_body_refresh(self) -> None:
        store = SnapshotStore()
        store.add("A", size=120)
        store.add("B", size=120)
        store.add("D", size=1)
        store.edges["B"].append("D")
        queue = MaterialQueue(1800)
        first = queue.next_package(store)
        if first is None:
            self.fail("Expected a complete material package")
        self.assertIn("A", first.writable_ids)
        self.assertNotIn("B", first.writable_ids)
        queue.accept(store, first.id)
        second = queue.next_package(store)
        if second is None:
            self.fail("Expected a complete material package")
        self.assertIn("D", second.read_only_ids)
        cast("Document", store.entries["D"]["content"])["appearance"] = "依赖正文发生实质变化"
        refreshed = queue.next_package(store)
        if refreshed is None:
            self.fail("Expected a complete material package")
        self.assertEqual(refreshed.id, second.id)
        self.assertGreater(refreshed.version, second.version)
        self.assertFalse(queue.is_current(store, second))
        self.assertIn("依赖正文发生实质变化", json.dumps(refreshed.business_payload(), ensure_ascii=False))

    def test_oversized_dependency_closure_deferred_once_then_other_units_continue(self) -> None:
        store = SnapshotStore()
        store.add("large", size=1000)
        store.add("dependent", dependencies=("large",))
        store.add("small")
        queue = MaterialQueue(1200)
        package = queue.next_package(store)
        if package is None:
            self.fail("Expected a complete material package")
        self.assertIn("small", package.writable_ids)
        self.assertEqual({entry["ids"][0] for entry in queue.deferred}, {"large", "dependent"})
        queue.accept(store, package.id)
        self.assertIsNone(queue.next_package(store))
        self.assertIsNone(queue.next_package(store))
        self.assertEqual(len(queue.deferred), 2)
        self.assertTrue(all(entry["reason"] == "input_budget_exceeded" for entry in queue.deferred))

    def test_new_reviewed_content_does_not_loop_but_explicit_new_gap_is_queued(self) -> None:
        store = SnapshotStore()
        store.add("A")
        queue = MaterialQueue(100_000)
        package = queue.next_package(store)
        if package is None:
            self.fail("Expected a complete material package")
        store.add("new")
        queue.accept(store, package.id)
        self.assertIsNone(queue.next_package(store))
        queue.enqueue_review(store, ["new:C", "new"])
        next_package = queue.next_package(store)
        if next_package is None:
            self.fail("Expected a complete material package")
        self.assertEqual(set(next_package.writable_ids), {"new", "new:C"})
        queue.accept(store, next_package.id)
        store.add("later_external")
        self.assertIsNotNone(queue.next_package(store))

    def test_unrelated_revision_does_not_invalidate_and_issue_change_does(self) -> None:
        store = SnapshotStore()
        store.add("A")
        queue = MaterialQueue(100_000, context={"issues": []})
        package = queue.next_package(store)
        if package is None:
            self.fail("Expected a complete material package")
        store.revision += 1
        self.assertTrue(queue.is_current(store, package))
        queue.update_context({"issues": ["初稿漏项"]})
        self.assertFalse(queue.is_current(store, package))
        refreshed = queue.next_package(store)
        if refreshed is None:
            self.fail("Expected a complete material package")
        self.assertEqual(refreshed.version, package.version + 1)

    def test_actual_request_overflow_defers_and_snapshot_is_independent(self) -> None:
        store = SnapshotStore()
        store.add("A")
        queue = MaterialQueue(100_000)
        self.assertIsNotNone(queue.next_package(store))
        snapshot = queue.snapshot()
        cast("list[str]", snapshot["pending"]).clear()
        queue.defer("repair_budget_exceeded")
        self.assertIsNone(queue.next_package(store))
        self.assertEqual(queue.deferred[0]["ids"], ["A"])
        self.assertEqual(queue.deferred[0]["reason"], "repair_budget_exceeded")

    def test_prior_dependency_change_requires_review_once_without_closure_loop(self) -> None:
        store = SnapshotStore()
        store.add("A", dependencies=("D",))
        store.add("D", dependencies=("A",))
        queue = MaterialQueue(100_000)
        initial = queue.next_package(store)
        if initial is None:
            self.fail("Expected initial package")
        queue.accept(store, initial.id)
        queue.enqueue_review(store, ["D"])
        current = queue.next_package(store)
        if current is None:
            self.fail("Expected D review")
        self.assertNotIn("A", current.writable_ids)
        cast("Document", store.entries["D"]["content"])["appearance"] = "不同现象"
        queue.accept(store, current.id)
        review = queue.next_package(store)
        if review is None:
            self.fail("Expected substantive review")
        self.assertIn("A", review.writable_ids)
        self.assertNotIn("D", review.writable_ids)
        self.assertEqual(queue.snapshot()["reviews"], [{"ids": ["A"], "reason": "substantive_material_change"}])
        queue.accept(store, review.id)
        self.assertIsNone(queue.next_package(store))

    def test_mechanical_alias_rewrite_does_not_reopen_completed_judgment(self) -> None:
        store = SnapshotStore()
        store.add("A", dependencies=("D",))
        store.add("D")
        queue = MaterialQueue(100_000)
        initial = queue.next_package(store)
        if initial is None:
            self.fail("Expected initial package")
        queue.accept(store, initial.id)
        queue.enqueue_review(store, ["D"])
        current = queue.next_package(store)
        if current is None:
            self.fail("Expected D review")
        store.aliases.update({"D": "renamed", "D:C": "renamed:C"})
        for old, new in store.aliases.items():
            entry = store.entries.pop(old)
            store.entries[new] = json.loads(json.dumps(entry).replace('"D"', '"renamed"').replace('"D:C"', '"renamed:C"'))
            store.edges[new] = store.edges.pop(old)
        queue.accept(store, current.id)
        self.assertIsNone(queue.next_package(store))
        self.assertEqual(queue.snapshot()["reviews"], [])

    def test_added_candidate_set_reopens_dependent_only_not_current_owner(self) -> None:
        store = SnapshotStore()
        store.add("A", dependencies=("D",))
        store.add("D")
        queue = MaterialQueue(100_000)
        initial = queue.next_package(store)
        if initial is None:
            self.fail("Expected initial package")
        queue.accept(store, initial.id)
        queue.enqueue_review(store, ["D"])
        current = queue.next_package(store)
        if current is None:
            self.fail("Expected D review")
        store.entries["D:new"] = {"handle": "D:new", "kind": "candidate", "owner": "D", "content": {"id": "D:new", "mechanism": "新的独立机制"}}
        cast("list[str]", store.entries["D"]["candidate_ids"]).append("D:new")
        store.edges["D"].append("D:new")
        queue.accept(store, current.id)
        review = queue.next_package(store)
        if review is None:
            self.fail("Expected candidate-set review")
        self.assertEqual(set(review.writable_ids), {"A", "A:C"})
        self.assertIn("D:new", review.read_only_ids)
        queue.accept(store, review.id)
        self.assertIsNone(queue.next_package(store))

    def test_real_duplicate_candidate_merge_is_only_mechanical_for_previous_sketch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LibraryStore(outline(), Path(temporary))
            original = report()
            feature = original.feature_library[0]
            duplicate = replace(feature.candidates[0], id="U")
            same = replace(original, feature_library=(replace(feature, candidates=(feature.candidates[0], duplicate)),))
            store.add_report("one", same)
            queue = MaterialQueue(100_000)
            initial = queue.next_package(store)
            if initial is None:
                self.fail("Expected initial package")
            queue.accept(store, initial.id)
            queue.enqueue_review(store, ["one::F"])
            current = queue.next_package(store)
            if current is None:
                self.fail("Expected owner review")
            store.present_materials(current.versions)
            store.merge("candidate", ["one::C", "one::U"], "one::C")
            queue.accept(store, current.id)
            self.assertIsNone(queue.next_package(store))
            self.assertEqual(queue.snapshot()["reviews"], [])

    def test_real_library_merge_refreshes_later_package_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LibraryStore(outline(), Path(temporary))
            store.add_report("one", report())
            store.add_report("two", report("平面亮线"))
            complete = MaterialQueue(100_000).next_package(store)
            if complete is None:
                self.fail("Expected complete source materials")
            features = store.material_closure(["one::F", "two::F"])
            entries = store.material_entries()
            feature_package = replace(
                complete,
                writable_ids=tuple(sorted(features)),
                read_only_ids=(),
                entries=tuple(entries[identifier] for identifier in features),
            )
            queue = MaterialQueue(feature_package.material_chars)
            first = queue.next_package(store)
            if first is None:
                self.fail("Expected a complete material package")
            self.assertIn("one::F", first.writable_ids)
            self.assertIn("two::F", first.writable_ids)
            store.present_materials(first.versions)
            store.merge("feature", ["one::F", "two::F"], "one::F")
            queue.accept(store, first.id)
            following = queue.next_package(store)
            if following is None:
                self.fail("Expected a complete material package")
            rendered = json.dumps(following.business_payload(), ensure_ascii=False)
            self.assertNotIn('"feature_id": "two::F"', rendered)
            self.assertIn('"feature_id": "one::F"', rendered)
            self.assertIn("two::C", following.content_versions)
