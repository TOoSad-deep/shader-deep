"""可能性库并集、呈现边界与原子整合的可观察行为."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langchain.messages import ToolMessage
from pydantic import TypeAdapter

from shader_deep.domain.library.models import Candidate, ExplorationReport, FeatureChoice, Participant, Relation, Requirement, Sketch, VisualOutline
from shader_deep.infrastructure.storage.library import FileLibraryStore as LibraryStore


def outline() -> VisualOutline:
    return TypeAdapter(VisualOutline).validate_python(
        {"elements": [{"id": "E", "name": "条带", "scope": "全图", "salient_features": []}], "relations": []}
    )


def report(mechanism: str = "曲面") -> ExplorationReport:
    return TypeAdapter(ExplorationReport).validate_python(
        {
            "sketch_library": [
                {
                    "id": "S",
                    "name": "组成",
                    "element_ids": ["E"],
                    "composition": ["重复单元"],
                    "feature_refs": [{"feature_id": "F", "candidate_ids": ["C"]}],
                }
            ],
            "feature_library": [
                {
                    "id": "F",
                    "name": "亮边",
                    "element_ids": ["E"],
                    "appearance": "边缘亮",
                    "candidates": [
                        {"id": "C", "mechanism": mechanism, "reasoning": "可产生渐变"},
                        {"id": "U", "mechanism": "独特但未使用", "reasoning": "仍待验证"},
                    ],
                }
            ],
            "relation_library": [],
        }
    )


def present(store: LibraryStore, identifier: str, call_id: str = "read") -> None:
    body = store.read(identifier, call_id)
    store.on_prepared([ToolMessage(content=body, tool_call_id=call_id)])


class PossibilityLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = LibraryStore(outline(), Path(self.temporary.name))

    def test_namespaces_preserve_elements_and_unreferenced_candidates(self) -> None:
        self.store.add_report("one", report())
        self.store.add_report("two", report("线条"))
        self.assertEqual(len(self.store.library.feature_library), 2)
        self.assertEqual(self.store.library.feature_library[0].element_ids, ("E",))
        self.assertEqual(self.store.library.sketch_library[0].feature_refs[0].candidate_refs[0].candidate_id, "one::C")
        entries = self.store.catalog(limit=100)["entries"]
        self.assertIn("two::U", [entry["handle"] for entry in entries])
        self.assertEqual(self.store.catalog(limit=2)["next_offset"], 2)

    def test_report_is_immutable_and_same_import_idempotent(self) -> None:
        self.store.add_report("one", report())
        original = (self.store.directory / "source-one.json").read_text()
        self.store.add_report("one", report())
        self.assertEqual(self.store.revision, 1)
        with self.assertRaisesRegex(ValueError, "Immutable report"):
            self.store.add_report("one", report("other"))
        self.assertEqual((self.store.directory / "source-one.json").read_text(), original)

    def test_catalog_and_successful_read_do_not_prove_presentation(self) -> None:
        self.store.add_report("one", report())
        self.store.add_report("two", report())
        body = self.store.read("one::S", "read")
        self.assertFalse(self.store.presented)
        self.store.on_prepared([ToolMessage(content=body[:20], tool_call_id="read")])
        self.assertFalse(self.store.presented)
        self.store.on_prepared([ToolMessage(content=body, tool_call_id="wrong")])
        self.assertFalse(self.store.presented)
        with self.assertRaisesRegex(ValueError, "Read and present"):
            self.store.merge("feature", ["one::F", "two::F"], "one::F")
        self.store.on_prepared([ToolMessage(content=body, tool_call_id="read")])
        self.assertIn("one::C", self.store.presented)
        self.assertNotIn("one::U", self.store.presented)
        self.assertNotIn("two::F", self.store.presented)

    def test_explicit_owner_merge_keeps_mechanisms_rewrites_refs_and_aliases(self) -> None:
        self.store.add_report("one", report())
        self.store.add_report("two", report("细线"))
        present(self.store, "one::S")
        present(self.store, "two::S")
        present(self.store, "one::F")
        present(self.store, "two::F")
        self.store.merge("feature", ["one::F", "two::F"], "one::F")
        self.assertEqual(len(self.store.library.feature_library), 1)
        self.assertEqual(len(self.store.library.feature_library[0].candidates), 4)
        self.assertEqual(self.store.library.sketch_library[1].feature_refs[0].feature_id, "one::F")
        self.assertEqual(self.store.resolve("two::F"), "one::F")
        self.assertEqual(len(self.store.provenance["one::F"]), 2)
        self.assertIn('"handle":"one::F"', self.store.read("two::F"))

    def test_candidate_merge_rewrites_uses_and_retains_sources(self) -> None:
        self.store.add_report("one", report())
        present(self.store, "one::S")
        present(self.store, "one::U")
        self.store.merge("candidate", ["one::C", "one::U"], "one::U")
        self.assertEqual(self.store.library.sketch_library[0].feature_refs[0].candidate_refs[0].candidate_id, "one::U")
        self.assertEqual(len(self.store.provenance["one::U"]), 2)
        self.assertEqual(len(self.store.library.feature_library[0].candidates), 1)

    def test_invalid_update_does_not_publish_partial_state(self) -> None:
        self.store.add_report("one", report())
        present(self.store, "one::S")
        prior, revision = self.store.library, self.store.revision
        with self.assertRaises(ValueError):
            self.store.update_sketch(replace(prior.sketch_library[0], element_ids=("missing",)))
        self.assertEqual(self.store.library, prior)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(len(list(self.store.directory.glob("version-*.json"))), 1)

    def test_main_additions_have_separate_authorship(self) -> None:
        handles = self.store.add(report())
        self.assertEqual(self.store.provenance[handles[0]][0]["author"], "main")

    def test_partial_response_never_marks_pending_objects_presented(self) -> None:
        self.store.add_report("one", report())
        self.store.max_response_chars = 550
        body = self.store.read("one::S", "read")
        response = json.loads(body)
        self.assertFalse(response["complete"])
        self.assertTrue(response["pending_handles"] or response["oversized_handles"])
        self.store.on_prepared([ToolMessage(content=body, tool_call_id="read")])
        self.assertEqual(self.store.presented, {item["handle"] for item in response["objects"]})
        self.assertLessEqual(len(body), self.store.max_response_chars)

    def test_prerequisite_cycle_reads_once_and_rewrites_on_merge(self) -> None:
        source = report()
        first, second = source.feature_library[0].candidates
        first = replace(first, requires=(Requirement(kind="feature", id="F", candidate_ids=("U",)),))
        second = replace(second, requires=(Requirement(kind="feature", id="F", candidate_ids=("C",)),))
        source = replace(source, sketch_library=(), feature_library=(replace(source.feature_library[0], candidates=(first, second)),))
        self.store.add_report("one", source)
        payload = json.loads(self.store.read("one::C", "cycle"))
        self.assertEqual(len(payload["objects"]), 3)
        self.assertTrue(payload["complete"])
        self.store.on_prepared([ToolMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")), tool_call_id="cycle")])
        with self.assertRaisesRegex(ValueError, "different prerequisites"):
            self.store.merge("candidate", ["one::C", "one::U"], "one::C")

    def test_failure_during_snapshot_write_does_not_publish_memory(self) -> None:
        self.store.add_report("one", report())
        before = self.store.library
        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")), self.assertRaises(OSError):
            self.store.add_report("two", report())
        self.assertEqual(self.store.library, before)
        self.assertEqual(self.store.report_ids, ("one",))
        self.assertFalse(list(self.store.directory.glob("*.tmp")))
        self.store.add_report("two", report())
        self.assertEqual(len(self.store.library.feature_library), 2)

    def test_main_can_add_cross_report_composition_after_reading_sources(self) -> None:
        self.store.add_report("one", report())
        addition = ExplorationReport(
            sketch_library=(
                Sketch(
                    id="new",
                    name="新增组成",
                    element_ids=("E",),
                    composition=("共享原候选",),
                    feature_refs=(FeatureChoice(feature_id="one::F", candidate_ids=("one::U",)),),
                ),
            ),
            feature_library=(),
            relation_library=(),
        )
        with self.assertRaisesRegex(ValueError, "Read and present"):
            self.store.add(addition)
        present(self.store, "one::F")
        self.store.add(addition)
        self.assertEqual(self.store.library.sketch_library[-1].feature_refs[0].feature_id, "one::F")
        self.assertEqual(self.store.library.sketch_library[-1].id, "main-1::new")

    def test_sketch_merge_unions_scope_choices_without_pruning_candidates(self) -> None:
        self.store.add_report("one", report())
        self.store.add_report("two", report())
        present(self.store, "one::S")
        present(self.store, "two::S")
        present(self.store, "one::F")
        present(self.store, "two::F")
        self.store.merge("feature", ["one::F", "two::F"], "one::F")
        present(self.store, "one::S")
        present(self.store, "two::S")
        self.store.merge("sketch", ["one::S", "two::S"], "one::S")
        refs = self.store.library.sketch_library[0].feature_refs
        self.assertEqual(len(refs), 1)
        self.assertEqual({item.candidate_id for item in refs[0].candidate_refs}, {"one::C", "two::C"})
        self.assertEqual(len(self.store.library.feature_library[0].candidates), 4)

    def test_outline_migration_requires_read_and_preserves_raw_source(self) -> None:
        self.store.add_report("one", report())
        self.store.update_elements_unresolved({"E": ["暗部未解"]})
        revised = replace(outline(), elements=(replace(outline().elements[0], id="E2"),))
        original = (self.store.directory / "source-one.json").read_text()
        with self.assertRaisesRegex(ValueError, "Read and present"):
            self.store.revise_outline(revised, {"E": "E2"}, {})
        present(self.store, "one::S")
        present(self.store, "one::F")
        self.store.revise_outline(revised, {"E": "E2"}, {})
        self.assertEqual(self.store.library.feature_library[0].element_ids, ("E2",))
        self.assertEqual(self.store.library.sketch_library[0].element_ids, ("E2",))
        self.assertEqual(self.store.library.elements[0].unresolved, ("暗部未解",))
        self.assertEqual((self.store.directory / "source-one.json").read_text(), original)
        self.assertEqual(self.store.outline, revised)
        snapshot = json.loads((self.store.directory / f"version-{self.store.revision:04d}.json").read_text())
        self.assertEqual(snapshot["outline"]["elements"][0]["id"], "E2")

    def test_outline_removing_referenced_identity_is_atomic_failure(self) -> None:
        self.store.add_report("one", report())
        revised = replace(outline(), elements=(replace(outline().elements[0], id="E2"),))
        before = self.store.library
        with self.assertRaises(ValueError):
            self.store.revise_outline(revised, {}, {})
        self.assertEqual(self.store.library, before)
        self.assertEqual(self.store.outline, outline())

    def test_huge_closure_uses_bounded_readable_continuations(self) -> None:
        source = report()
        base = source.feature_library[0].candidates[0]
        candidates = tuple(replace(base, id=f"long-candidate-{index:04d}") for index in range(100))
        source = replace(source, sketch_library=(), feature_library=(replace(source.feature_library[0], candidates=candidates),))
        self.store.add_report("one", source)
        self.store.max_response_chars = 700
        body = self.store.read("one::F", "first")
        self.assertLessEqual(len(body), self.store.max_response_chars)
        response = json.loads(body)
        self.assertTrue(response["pending_handles"])
        next_body = self.store.read(response["pending_handles"][0], "next")
        self.assertLessEqual(len(next_body), self.store.max_response_chars)
        self.assertNotEqual(body, next_body)

    def test_feature_merge_cannot_broaden_applicability_silently(self) -> None:
        revised = replace(outline(), elements=(*outline().elements, replace(outline().elements[0], id="E2")))
        self.store.revise_outline(revised, {}, {})
        self.store.add_report("one", report())
        other = report()
        other = replace(other, sketch_library=(), feature_library=(replace(other.feature_library[0], element_ids=("E2",)),))
        self.store.add_report("two", other)
        present(self.store, "one::F")
        present(self.store, "two::F")
        before = self.store.library
        with self.assertRaisesRegex(ValueError, "different elements"):
            self.store.merge("feature", ["one::F", "two::F"], "one::F")
        self.assertEqual(self.store.library, before)

    def test_merge_rewrites_relation_endpoints_and_candidate_requirements(self) -> None:
        source = report()
        relation = Relation(
            id="R",
            name="影响",
            kind="dependency",
            participants=(Participant(kind="feature", id="F", role="source"), Participant(kind="element", id="E", role="target")),
            description="亮边影响外观",
            candidates=(
                Candidate(id="RC", mechanism="共享渐变", reasoning="连贯性", requires=(Requirement(kind="feature", id="F", candidate_ids=("C",)),)),
            ),
        )
        source = replace(source, relation_library=(relation,))
        self.store.add_report("one", source)
        self.store.add_report("two", source)
        present(self.store, "one::R")
        present(self.store, "two::R")
        present(self.store, "one::F")
        present(self.store, "two::F")
        self.store.merge("feature", ["one::F", "two::F"], "one::F")
        migrated = self.store.library.relation_library[1]
        self.assertEqual(migrated.participants[0].id, "one::F")
        self.assertEqual(migrated.participants[1].id, "E")
        self.assertEqual(migrated.candidates[0].requires[0].id, "one::F")
        present(self.store, "one::R")
        present(self.store, "two::R")
        self.store.merge("candidate", ["one::C", "two::C"], "one::C")
        self.assertEqual(self.store.library.relation_library[1].candidates[0].requires[0].candidate_ids, ("one::C",))

    def test_outline_cannot_discard_unresolved_element(self) -> None:
        initial = outline()
        extra = replace(initial, elements=(*initial.elements, replace(initial.elements[0], id="E2")))
        self.store.revise_outline(extra, {}, {})
        self.store.update_elements_unresolved({"E2": ["critical unexplained detail"]})
        before = self.store.library
        with self.assertRaisesRegex(ValueError, "unresolved problems"):
            self.store.revise_outline(initial, {}, {})
        self.assertEqual(self.store.library, before)

    def test_catalog_respects_transport_budget_and_still_discovers_every_candidate(self) -> None:
        self.store.add_report("one", report())
        self.store.max_response_chars = 500
        seen, offset = set(), 0
        while offset is not None:
            page = self.store.catalog(offset, limit=10000)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), self.store.max_response_chars)
            seen.update(entry["handle"] for entry in page["entries"])
            offset = page["next_offset"]
        self.assertEqual(seen, {"one::S", "one::F", "one::C", "one::U"})

    def test_oversized_candidate_fragments_are_lossless_and_require_full_presentation(self) -> None:
        source = report()
        original = '引号"反斜线\\换行\n' * 10000
        candidate = replace(source.feature_library[0].candidates[0], mechanism=original)
        source = replace(source, feature_library=(replace(source.feature_library[0], candidates=(candidate,)),))
        self.store.add_report("one", source)
        pending, fragments, serial = ["one::C"], [], 0
        while pending:
            serial += 1
            self.assertLess(serial, 100)
            body = self.store.read(pending.pop(0), f"chunk-{serial}")
            self.assertLessEqual(len(body), self.store.max_response_chars)
            response = json.loads(body)
            fragments.extend(item for item in response["objects"] if item["kind"] == "json_fragment")
            pending.extend(response["pending_handles"])
            if serial == 1:
                self.assertNotIn("one::C", self.store.presented)
                self.store.on_prepared([ToolMessage(content=body[:20], tool_call_id=f"chunk-{serial}")])
                self.assertNotIn("one::C", self.store.presented)
            self.store.on_prepared([ToolMessage(content=body, tool_call_id=f"chunk-{serial}")])
            if sum(len(item["text"]) for item in fragments) < fragments[0]["total_chars"]:
                self.assertNotIn("one::C", self.store.presented)
        reconstructed = json.loads("".join(item["text"] for item in sorted(fragments, key=lambda item: item["offset"])))
        self.assertEqual(reconstructed["content"]["mechanism"], candidate.mechanism)
        self.assertIn("one::C", self.store.presented)

    def test_fragment_presentation_does_not_mark_modified_source_version_read(self) -> None:
        source = report()
        source = replace(source, sketch_library=(replace(source.sketch_library[0], composition=("x" * 80000,)),))
        self.store.add_report("one", source)
        pending, serial = ["one::S"], 0
        while pending:
            serial += 1
            self.assertLess(serial, 20)
            body = self.store.read(pending.pop(0), f"complete-{serial}")
            pending.extend(json.loads(body)["pending_handles"])
            self.store.on_prepared([ToolMessage(content=body, tool_call_id=f"complete-{serial}")])
        self.assertIn("one::S", self.store.presented)
        stale = self.store.read("one::S", "stale")
        present(self.store, "one::F")
        self.store.update_sketch(replace(self.store.library.sketch_library[0], name="clarified name"))
        self.assertNotIn("one::S", self.store.presented)
        self.store.on_prepared([ToolMessage(content=stale, tool_call_id="stale")])
        self.assertNotIn("one::S", self.store.presented)

    def test_update_sketch_cannot_replace_a_distinct_composition_or_element_scope(self) -> None:
        self.store.add_report("one", report())
        expanded = replace(outline(), elements=(*outline().elements, replace(outline().elements[0], id="E2")))
        self.store.revise_outline(expanded, {}, {})
        present(self.store, "one::S")
        present(self.store, "one::F")
        before, revision = self.store.library, self.store.revision
        current = before.sketch_library[0]
        for replacement in (replace(current, composition=("different mechanism",)), replace(current, element_ids=("E", "E2"))):
            with self.subTest(composition=replacement.composition, element_ids=replacement.element_ids):
                with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                    self.store.update_sketch(replacement)
                self.assertEqual(self.store.library, before)
                self.assertEqual(self.store.revision, revision)
        self.store.update_sketch(replace(current, unresolved=("未解暗部",)))
        self.assertEqual(self.store.library.sketch_library[0].composition, current.composition)
        self.assertEqual(self.store.library.sketch_library[0].unresolved, ("未解暗部",))
