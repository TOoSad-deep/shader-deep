"""字符分页保持全部身份可发现, 不把未显示的必要工作视为完成."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from shader_deep.analysis.library import LibraryStore
from shader_deep.analysis.library_views import ReadRequest, list_library, select_library
from shader_deep.analysis.materials import ComparisonManager
from tests.unit_tests.test_integration_materials import SnapshotStore
from tests.unit_tests.test_possibility_library import outline, report


class BoundedViewsTests(unittest.TestCase):
    def test_empty_summary_uses_actual_metadata_budget(self) -> None:
        manager = ComparisonManager()
        summary = manager.summary(max_chars=512)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False)), 512)
        self.assertEqual(summary["works"], [])
        self.assertEqual(summary["pending_work_count"], 0)
        with self.assertRaisesRegex(ValueError, "metadata exceeds page budget"):
            manager.summary(max_chars=10)

    def test_library_char_pages_preserve_every_identity_and_full_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LibraryStore(outline(), Path(temporary))
            source = report()
            feature = replace(source.feature_library[0], name="很长的目录名称" * 1000)
            store.add_report("large", replace(source, feature_library=(feature,)))
            for index in range(12):
                store.add_report(f"small-{index}", source)
            cursor = None
            identifiers: list[str] = []
            omitted: list[str] = []
            while True:
                page = list_library(store, cursor=cursor, max_chars=1000)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 1000)
                self.assertTrue(page["entries"])
                identifiers.extend(row["id"] for row in page["entries"])
                omitted.extend(row["id"] for row in page["entries"] if row.get("details_omitted"))
                if cursor:
                    self.assertNotEqual(page["next_cursor"], cursor)
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            self.assertEqual(len(identifiers), len(set(identifiers)))
            self.assertEqual(set(identifiers), {key for key, value in store.material_entries().items() if value["kind"] != "candidate"})
            self.assertIn("large::F", omitted)
            selected = select_library(store, [ReadRequest(library="feature_library", ids=["large::F"])])
            self.assertEqual(selected["large::F"]["content"]["name"], feature.name)

    def test_pending_pages_are_bounded_without_changing_completion_obligations(self) -> None:
        store = SnapshotStore()
        store.add("F")
        manager = ComparisonManager()
        for index in range(35):
            manager.start(store, f"复核问题 {index}", ["F"])
            manager.defer("材料未齐")
        before = manager.snapshot()
        summary = manager.summary(max_chars=2000)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False)), 2000)
        self.assertLess(len(summary["works"]), 35)
        self.assertEqual(summary["pending_work_count"], 35)
        seen = {work["work_id"] for work in summary["works"]}
        cursor = summary["next_cursor"]
        while cursor:
            page = manager.list_work(cursor=cursor, max_chars=2000)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 2000)
            seen.update(work["work_id"] for work in page["works"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, {work["work_id"] for work in manager.pending})
        self.assertEqual(manager.snapshot(), before)

    def test_work_and_target_cursors_invalidate_on_ledger_status_change(self) -> None:
        store = SnapshotStore()
        for index in range(8):
            store.add(f"F{index}")
        manager = ComparisonManager()
        first = manager.start(store, "第一组", [f"F{index}" for index in range(8)])
        manager.defer("材料未齐")
        manager.start(store, "第二组", ["F0"])
        manager.defer("材料未齐")
        cursor = manager.list_work(limit=1)["next_cursor"]
        targets = manager.list_work(work_id=first.id, limit=2)["works"][0]
        manager.start(store, "", [], work_id=first.id)
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            manager.list_work(cursor=cursor)
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            manager.list_work(work_id=first.id, target_cursor=targets["target_next_cursor"])

    def test_large_work_details_are_explicit_and_targets_remain_pageable(self) -> None:
        store = SnapshotStore()
        for index in range(20):
            store.add(f"F{index}")
        manager = ComparisonManager()
        question = "完整问题不能被摘要代替" * 1000
        group = manager.start(store, question, [f"F{index}" for index in range(20)])
        manager.defer("完整原因" * 1000)
        page = manager.list_work(max_chars=1200)
        self.assertTrue(page["works"][0]["details_omitted"])
        self.assertEqual(page["works"][0]["target_query"], {"work_id": group.id})
        cursor = None
        seen: list[str] = []
        while True:
            page = manager.list_work(work_id=group.id, target_cursor=cursor, limit=3, max_chars=1200)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 1200)
            row = page["works"][0]
            self.assertTrue(row["details_unavailable_due_to_budget"])
            seen.extend(row["target_ids"])
            cursor = row["target_next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, list(group.target_ids))
        self.assertEqual(manager.pending[0]["question"], question)
        self.assertEqual(manager.list_work(work_id=group.id, max_chars=30000)["works"][0]["question"], question)

    def test_work_filter_does_not_allow_cursor_from_another_query(self) -> None:
        store = SnapshotStore()
        store.add("F")
        manager = ComparisonManager()
        for index in range(3):
            manager.start(store, f"问题 {index}", ["F"])
            manager.defer("等待材料")
        cursor = manager.list_work(statuses=["deferred"], limit=1)["next_cursor"]
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            manager.list_work(cursor=cursor)
        self.assertEqual(len(manager.pending), 3)

    def test_resume_keeps_complete_reason_and_gaps_beyond_the_visible_index(self) -> None:
        store = SnapshotStore()
        store.add("F")
        manager = ComparisonManager()
        group = manager.start(store, "继续核实外观", ["F"])
        group.gaps = [{"id": "F:C", "reason": "候选正文尚未核对"}]
        reason = "需要完整证据才能继续" * 1000
        manager.defer(reason)
        row = manager.list_work(work_id=group.id, max_chars=1200)["works"][0]
        self.assertTrue(row["details_unavailable_due_to_budget"])
        resumed = manager.start(store, "", [], work_id=group.id)
        context = resumed.business_payload()["work_context"]
        self.assertEqual(context, {"reason": reason, "gaps": [{"id": "F:C", "reason": "候选正文尚未核对"}]})
        manager.append({"F": store.material_entries()["F"]}, store.material_versions(["F"]), library_revision=store.revision)
        store.add("F", size=20)
        self.assertEqual(manager.refresh(store).business_payload()["work_context"], context)
