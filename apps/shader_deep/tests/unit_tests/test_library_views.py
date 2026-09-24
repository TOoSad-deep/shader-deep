"""按需目录、显式候选读取及上下文去重的无网络行为验证."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from shader_deep.analysis.exploration import Candidate, Participant, Relation, Requirement
from shader_deep.analysis.library import LibraryStore
from shader_deep.analysis.library_views import ComparisonSpec, ReadLibraryInput, ReadRequest, list_library, select_library
from shader_deep.context.exploration import build_integration_context
from tests.unit_tests.test_possibility_library import outline, report


class LibraryViewsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LibraryStore(outline(), Path(temporary.name))
        self.store.add_report("one", report())
        self.store.add_report("two", report("细线"))

    def test_sketch_and_owner_read_never_expand_candidate_bodies(self) -> None:
        selected = select_library(
            self.store,
            [ReadRequest(library="sketch_library", ids=["one::S"]), ReadRequest(library="feature_library", ids=["one::F"])],
        )
        self.assertEqual(list(selected), ["one::S", "one::F"])
        self.assertEqual(selected["one::F"]["candidate_ids"], ["one::C", "one::U"])
        self.assertNotIn("mechanism", json.dumps(selected))
        self.assertEqual(self.store.presented, set())

    def test_explicit_candidates_are_deduplicated_and_prerequisites_not_expanded(self) -> None:
        source = report()
        feature = source.feature_library[0]
        candidate = replace(feature.candidates[0], requires=(Requirement(kind="feature", id="F", candidate_ids=("U",)),))
        source = replace(source, sketch_library=(), feature_library=(replace(feature, candidates=(candidate, feature.candidates[1])),))
        self.store.add_report("requires", source)
        request = ReadRequest(library="feature_library", ids=["requires::F"], include_candidates=True, candidate_ids=["requires::C"])
        selected = select_library(self.store, [request, request])
        self.assertEqual(list(selected), ["requires::F", "requires::C"])
        self.assertIn("requires", selected["requires::C"]["content"])
        self.assertNotIn("requires::U", selected)
        self.assertEqual(selected["requires::C"]["owner"], "requires::F")

    def test_wrong_library_and_foreign_candidate_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown sketch_library"):
            select_library(self.store, [ReadRequest(library="sketch_library", ids=["one::F"])])
        request = ReadRequest(library="feature_library", ids=["one::F"], include_candidates=True, candidate_ids=["two::C"])
        with self.assertRaisesRegex(ValueError, "do not belong"):
            select_library(self.store, [request])
        with self.assertRaises(ValidationError):
            ReadRequest(library="feature_library", ids=["one::F"], candidate_ids=["one::C"])
        with self.assertRaises(ValidationError):
            ReadRequest(library="sketch_library", ids=["one::S"], include_candidates=True)
        with self.assertRaises(ValidationError):
            ReadLibraryInput(requests=[request], comparison=ComparisonSpec(question="检查亮边", target_ids=["one::F"]), work_id="work-1")

    def test_catalog_paging_hides_mechanisms_and_preserves_remaining_range(self) -> None:
        first = list_library(self.store, limit=2)
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["remaining"], 3)
        second = list_library(self.store, limit=2, cursor=first["next_cursor"])
        last = list_library(self.store, limit=2, cursor=second["next_cursor"])
        identifiers = [row["id"] for page in (first, second, last) for row in page["entries"]]
        self.assertEqual(len(set(identifiers)), 5)
        self.assertEqual(last["remaining"], 0)
        self.assertIsNone(last["next_cursor"])
        self.assertNotIn("mechanism", json.dumps([first, second, last]))
        self.assertNotIn("reasoning", json.dumps([first, second, last]))

    def test_catalog_filters_and_candidate_discovery_are_explicit(self) -> None:
        result = list_library(
            self.store, library="feature_library", element_ids=["E"], statuses=["preserved"], processing_status={"one::F": "preserved"}
        )
        self.assertEqual([row["id"] for row in result["entries"]], ["one::F"])
        self.assertEqual(result["entries"][0]["candidate_count"], 2)
        candidates = list_library(self.store, owner_ids=["one::F"])
        self.assertEqual([row["id"] for row in candidates["entries"]], ["one::C", "one::U"])
        self.assertTrue(all(row["owner_id"] == "one::F" for row in candidates["entries"]))
        self.assertEqual(list_library(self.store, element_ids=["unknown"])["total"], 0)

    def test_catalog_filters_feature_and_relation_endpoints_without_expansion(self) -> None:
        relation = Relation(
            id="R",
            name="连接",
            kind="organization",
            description="特征与元素相接",
            participants=(Participant(kind="feature", id="F"), Participant(kind="element", id="E")),
            candidates=(Candidate(id="RC", mechanism="相交", reasoning="共享边界"),),
        )
        dependent = Relation(
            id="Q",
            name="组织",
            kind="organization",
            description="关系与元素相连",
            participants=(Participant(kind="relation", id="R"), Participant(kind="element", id="E")),
            candidates=(Candidate(id="QC", mechanism="叠加", reasoning="局部相接"),),
        )
        self.store.add_report("relations", replace(report(), relation_library=(relation, dependent)))
        feature_rows = list_library(self.store, endpoint_ids=["relations::F"])["entries"]
        self.assertEqual([row["id"] for row in feature_rows], ["relations::R"])
        relation_rows = list_library(self.store, endpoint_ids=["relations::R"])["entries"]
        self.assertEqual([row["id"] for row in relation_rows], ["relations::Q"])
        candidate_rows = list_library(self.store, owner_ids=["relations::R"], endpoint_ids=["relations::F"])["entries"]
        self.assertEqual([row["id"] for row in candidate_rows], ["relations::RC"])
        self.assertNotIn("mechanism", json.dumps(feature_rows + relation_rows + candidate_rows))

    def test_catalog_rejects_cursor_after_library_progress_or_query_change(self) -> None:
        first = list_library(self.store, limit=1)
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            list_library(self.store, cursor=first["next_cursor"], processing_status={"one::F": "preserved"})
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            list_library(self.store, cursor=first["next_cursor"], library="feature_library")
        self.store.add_report("three", report())
        with self.assertRaisesRegex(ValueError, "Stale library cursor"):
            list_library(self.store, cursor=first["next_cursor"])
        with self.assertRaisesRegex(ValueError, "Invalid library cursor"):
            list_library(self.store, cursor="invalid")

    def test_element_read_is_complete_and_outline_does_not_duplicate_it(self) -> None:
        selected = select_library(self.store, [ReadRequest(library="elements", ids=["E"])])
        self.assertEqual(selected["E"]["content"]["scope"], "全图")
        index = {"catalog": list_library(self.store), "comparison": {"entries": list(selected.values())}}
        message = build_integration_context("比较", outline(), "data:image/png;base64,A", index=index)
        payload = json.loads(message.content[0]["text"])
        self.assertEqual(payload["visual_outline"]["elements"], [])
        self.assertEqual(payload["integration_materials"]["comparison"]["entries"][0]["content"]["scope"], "全图")
        self.assertEqual([item["type"] for item in message.content], ["text", "image_url"])
