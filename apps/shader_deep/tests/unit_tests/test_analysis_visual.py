"""保护视觉结构引用、局部修订与逐假设实现建议的兼容边界."""

from __future__ import annotations

from dataclasses import replace
from unittest import TestCase

from pydantic import TypeAdapter, ValidationError

from shader_deep.analysis.evidence import ImageRegion
from shader_deep.analysis.references import report_catalog, unlinked_interpretations
from shader_deep.analysis.schemas import (
    AnalysisSummary,
    AnalysisTaskRequest,
    HypothesisLink,
    ImplementationSketch,
    Interpretation,
    LensReport,
    Observation,
    RenderCheckpoint,
    SourcedStatement,
    SourceRef,
    VisualDecomposition,
    VisualElement,
    VisualFeature,
    VisualMapping,
    VisualRelation,
    VisualRevision,
)
from shader_deep.analysis.validation import (
    AnalysisValidationError,
    validate_analysis_result,
    validate_lens_visual,
    validate_summary_visual,
    validate_task_visual,
    validate_visual_decomposition,
    validate_visual_evidence,
)
from shader_deep.blackboard import new_blackboard
from shader_deep.schemas import ResultRecord, TaskRecord


def draft() -> VisualDecomposition:
    return VisualDecomposition(
        elements=(VisualElement(id="E1", name="Left area", region="left"), VisualElement(id="E2", name="Right area", region="right")),
        features=(VisualFeature(id="F1", element_ids=("E1", "E2"), description="Purple transition", region="center"),),
        relations=(VisualRelation(id="R1", element_ids=("E1", "E2"), description="Overlap"),),
    )


def report() -> LensReport:
    return LensReport(
        scope="Overlap",
        observations=(Observation(id="O1", text="A soft boundary", region="center", feature_ids=("F1", "F2")),),
        interpretations=(Interpretation(id="H1", text="Blur", supporting_observation_ids=("O1",)), Interpretation(id="H2", text="Color field")),
        visual_additions=VisualDecomposition(
            features=(VisualFeature(id="F2", element_ids=("E1",), description="Soft edge", region="left"),),
            relations=(VisualRelation(id="R2", element_ids=("E1", "E2"), description="Right area covers left"),),
        ),
        visual_revisions=(VisualRevision(target_ids=("F1",), proposal="Separate transition from edge softness"),),
        implementation_sketches=(
            ImplementationSketch(id="S1", hypothesis_ids=("H1",), description="Sample neighbors", feature_ids=("F2",)),
            ImplementationSketch(id="S2", hypothesis_ids=("H2",), description="Blend color fields", feature_ids=("F1",)),
        ),
    )


class AnalysisVisualTests(TestCase):
    def test_report_collects_local_reference_errors_for_one_repair(self) -> None:
        payload = {
            "scope": "Whole image",
            "observations": [{"id": "O1", "text": "Soft edge", "region": "center"}],
            "interpretations": [
                {"id": "I1", "text": "Blur", "supporting_observation_ids": ["O1"], "opposing_observation_ids": ["O1"]},
                {"id": "I2", "text": "Color field", "supporting_observation_ids": ["missing"], "opposing_observation_ids": ["other"]},
            ],
        }
        with self.assertRaises(ValidationError) as caught:
            TypeAdapter(LensReport).validate_python(payload)
        error = caught.exception.errors()[0]["ctx"]["error"]
        self.assertIsInstance(error, AnalysisValidationError)
        self.assertEqual(
            [issue["path"] for issue in error.issues],
            [
                "/interpretations/0/opposing_observation_ids",
                "/interpretations/1/supporting_observation_ids",
                "/interpretations/1/opposing_observation_ids",
            ],
        )
        self.assertTrue(all("Invalid observation references" in issue["message"] for issue in error.issues))
        # 身份重复是引用解析的前置错误, 不把歧义 ID 继续解释为某个观察.
        duplicated = {**payload, "observations": payload["observations"] * 2}
        with self.assertRaises(ValidationError) as caught:
            TypeAdapter(LensReport).validate_python(duplicated)
        error = caught.exception.errors()[0]["ctx"]["error"]
        self.assertEqual([issue["path"] for issue in error.issues], ["/observations/1/id"])

    def test_reference_catalog_and_typed_sources_preserve_legacy_input(self) -> None:
        state = new_blackboard()
        root = TaskRecord(id="root", role="analysis", target_version="T1", objective="Analyze")
        state["tasks"]["W1"] = replace(root, id="W1", parent_task_id="root")
        state["results"]["A1"] = ResultRecord(id="A1", task_id="W1", status="completed", summary="Report", analysis_detail=report())
        catalog = report_catalog(state["results"]["A1"])
        self.assertEqual({entry["item_id"] for entry in catalog}, {None, "O1", "H1", "H2", "F2", "R2"})
        self.assertEqual(next(entry["pointer"] for entry in catalog if entry["item_id"] == "F2"), "/visual_additions/features/0")
        summary = AnalysisSummary(
            source_result_ids=("A1",),
            key_observations=(SourcedStatement(text="Soft edge", source_refs=(SourceRef(result_id="A1", item_id="O1"),)),),
        )
        result = ResultRecord(id="final", task_id="root", status="completed", summary="Summary", analysis_detail=summary)
        validate_analysis_result(state, result, root)
        invalid = replace(
            summary,
            key_observations=(
                SourcedStatement(text="Blur", source_refs=(SourceRef(result_id="A1", item_id="H1", kind="observation"),)),
                SourcedStatement(text="Soft edge", source_refs=(SourceRef(result_id="A1", item_id="O1", kind="interpretation"),)),
            ),
        )
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_analysis_result(state, replace(result, analysis_detail=invalid), root)
        issues = caught.exception.issues
        self.assertEqual(
            [item["path"] for item in issues], ["/summary/key_observations/0/source_refs/0", "/summary/key_observations/1/source_refs/0"]
        )
        self.assertEqual([item["code"] for item in issues], ["source_kind_mismatch", "source_kind_mismatch"])
        self.assertEqual(issues[0]["candidates"], [next(entry for entry in catalog if entry["item_id"] == "O1")])
        unsupported = replace(summary.key_observations[0], basis="measurement_supported", evidence_ids=("missing-first", "missing-second"))
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_analysis_result(state, replace(result, analysis_detail=replace(summary, key_observations=(unsupported,))), root)
        self.assertEqual(
            [issue["path"] for issue in caught.exception.issues],
            ["/summary/key_observations/0/evidence_ids/0", "/summary/key_observations/0/evidence_ids/1"],
        )
        self.assertTrue(all(issue["code"] == "evidence_outside_analysis" for issue in caught.exception.issues))

    def test_hypothesis_links_keep_report_namespaces_and_unlinked_alternatives(self) -> None:
        state = new_blackboard()
        root = TaskRecord(id="root", role="analysis", target_version="T1", objective="Analyze")
        for identifier in ("A1", "A2"):
            state["tasks"][identifier] = replace(root, id=identifier, parent_task_id="root")
            state["results"][identifier] = ResultRecord(
                id=identifier, task_id=identifier, status="completed", summary="Report", analysis_detail=report()
            )
        sources = (SourceRef(result_id="A1", item_id="O1"),)
        summary = AnalysisSummary(
            source_result_ids=("A1", "A2"),
            key_observations=(SourcedStatement(text="Soft edge", source_refs=sources),),
            hypotheses=(SourcedStatement(id="blur", text="Blur", source_refs=sources),),
            hypothesis_links=(
                HypothesisLink(
                    hypothesis_id="blur",
                    derived_from=(SourceRef(result_id="A1", item_id="H1", kind="interpretation"), SourceRef(result_id="A2", item_id="H1")),
                ),
            ),
        )
        result = ResultRecord(id="final", task_id="root", status="completed", summary="Summary", analysis_detail=summary)
        validate_analysis_result(state, result, root)
        self.assertEqual([(entry["result_id"], entry["item_id"]) for entry in unlinked_interpretations(state, summary)], [("A1", "H2"), ("A2", "H2")])
        self.assertEqual(len(unlinked_interpretations(state, replace(summary, hypothesis_links=()))), 4)
        invalid = replace(summary, hypothesis_links=(HypothesisLink(hypothesis_id="missing", derived_from=sources),))
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_analysis_result(state, replace(result, analysis_detail=invalid), root)
        self.assertEqual(
            [issue["path"] for issue in caught.exception.issues],
            ["/summary/hypothesis_links/0/hypothesis_id", "/summary/hypothesis_links/0/derived_from/0"],
        )

    def test_legacy_reports_keep_missing_visual_structure(self) -> None:
        old = TypeAdapter(LensReport).validate_python({"scope": "Whole", "observations": [{"id": "O1", "text": "Circle", "region": "center"}]})
        self.assertIsNone(old.visual_additions)
        validate_lens_visual(old)
        summary = AnalysisSummary(
            source_result_ids=("R1",), key_observations=(SourcedStatement(text="Circle", source_refs=(SourceRef(result_id="R1"),)),)
        )
        self.assertIsNone(summary.visual_decomposition)
        validate_summary_visual(new_blackboard(), summary)

    def test_shared_feature_and_local_addition_resolve_against_frozen_draft(self) -> None:
        initial, local = draft(), report()
        validate_lens_visual(local, initial)
        self.assertEqual(initial.features[0].element_ids, ("E1", "E2"))
        with self.assertRaisesRegex(ValueError, "visual element"):
            validate_lens_visual(local)
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_lens_visual(replace(local, visual_additions=initial), initial)
        collision = VisualDecomposition(elements=(VisualElement(id="O1", name="Boundary", region="center"),))
        with self.assertRaisesRegex(ValueError, "collide with report item IDs"):
            validate_lens_visual(replace(local, visual_additions=collision), initial)
        with self.assertRaisesRegex(ValueError, "revision target"):
            validate_lens_visual(replace(local, visual_revisions=(VisualRevision(target_ids=("missing",), proposal="Split"),)), initial)

    def test_dangling_and_cyclic_parent_relations_are_rejected(self) -> None:
        element = draft().elements[0]
        for elements in ((replace(element, parent_id="missing"),), (replace(element, parent_id="E1"),)):
            with self.subTest(elements=elements), self.assertRaisesRegex(ValueError, "cycle or missing"):
                validate_visual_decomposition(VisualDecomposition(elements=elements))
        with self.assertRaisesRegex(ValueError, "visual element"):
            validate_visual_decomposition(replace(draft(), relations=(VisualRelation(id="R2", element_ids=("E1", "E1"), description="Repeated"),)))

    def test_focus_and_sketches_cannot_reference_unknown_objects_or_hypotheses(self) -> None:
        request = AnalysisTaskRequest(preset_id="graphics-2d", objective="Compare", focus_feature_ids=("F1",))
        validate_task_visual(request, draft())
        with self.assertRaisesRegex(ValueError, "focus feature"):
            validate_task_visual(request, None)
        local = report()
        with self.assertRaisesRegex(ValueError, "hypothesis"):
            validate_lens_visual(
                replace(local, implementation_sketches=(replace(local.implementation_sketches[0], hypothesis_ids=("missing",)),)), draft()
            )
        with self.assertRaisesRegex(ValueError, "sketch IDs"):
            validate_lens_visual(replace(local, implementation_sketches=(local.implementation_sketches[0],) * 2), draft())
        invalid = replace(
            local,
            observations=(replace(local.observations[0], element_ids=("missing-element",), feature_ids=("missing-feature",)),),
            implementation_sketches=(replace(local.implementation_sketches[0], hypothesis_ids=("missing-hypothesis",)),),
        )
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_lens_visual(invalid, draft())
        self.assertEqual(
            [issue["path"] for issue in caught.exception.issues],
            [
                "/report/observations/0/element_ids/0",
                "/report/observations/0/feature_ids/0",
                "/report/implementation_sketches/0/hypothesis_ids/0",
            ],
        )
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_lens_visual(replace(invalid, visual_additions=draft()), draft())
        self.assertTrue(all(issue["code"] == "duplicate_visual_id" for issue in caught.exception.issues))

    def test_pixel_regions_reject_out_of_image_bounds(self) -> None:
        initial = draft()
        region = ImageRegion(left=0, top=0, right=101, bottom=10)
        with self.assertRaisesRegex(ValueError, "outside image"):
            validate_visual_decomposition(
                replace(initial, elements=(replace(initial.elements[0], region_box=region), initial.elements[1])), image_size=(100, 100)
            )
        local = report()
        sketch = replace(local.implementation_sketches[0], render_checkpoints=(RenderCheckpoint(region="edge", compare="width", region_box=region),))
        with self.assertRaisesRegex(ValueError, "outside image"):
            validate_lens_visual(replace(local, implementation_sketches=(sketch,)), initial, image_size=(100, 100))

    def test_unmeasured_visual_items_cannot_claim_numeric_support(self) -> None:
        state = new_blackboard()
        task = TaskRecord(id="A1", role="analysis", target_version="T1", objective="Analyze")
        unsupported = VisualDecomposition(elements=(VisualElement(id="E1", name="Area", region="center", basis="measurement_supported"),))
        with self.assertRaisesRegex(ValueError, "numeric measurement"):
            validate_visual_evidence(state, task, unsupported)
        foreign = replace(unsupported, elements=(replace(unsupported.elements[0], evidence_ids=("foreign",)),))
        with self.assertRaisesRegex(ValueError, "outside analysis"):
            validate_visual_evidence(state, task, foreign)

    def test_summary_mapping_keeps_report_local_namespaces_and_hypothesis_branches(self) -> None:
        state = new_blackboard()
        state["results"]["A1"] = ResultRecord(id="A1", task_id="W1", status="completed", summary="First", analysis_detail=report())
        state["results"]["A2"] = ResultRecord(
            id="A2",
            task_id="W2",
            status="completed",
            summary="Second",
            analysis_detail=replace(
                report(),
                visual_additions=VisualDecomposition(
                    features=(VisualFeature(id="F2", element_ids=("E2",), description="Alternative right edge", region="right"),)
                ),
            ),
        )
        source = (SourceRef(result_id="A1", item_id="O1"),)
        summary = AnalysisSummary(
            source_result_ids=("A1", "A2"),
            key_observations=(SourcedStatement(text="Softness", source_refs=source, feature_ids=("F1",)),),
            visual_decomposition=draft(),
            visual_mappings=(
                VisualMapping(source_result_id="A1", source_id="F2", target_id="F1", kind="feature"),
                VisualMapping(source_result_id="A2", source_id="F2", target_id="F1", kind="feature"),
            ),
            hypotheses=(
                SourcedStatement(id="H1", text="Blur", source_refs=source),
                SourcedStatement(id="H2", text="Color field", source_refs=source),
            ),
            implementation_sketches=(replace(report().implementation_sketches[0], feature_ids=("F1",)), report().implementation_sketches[1]),
        )
        validate_summary_visual(state, summary, decomposition=draft(), source_decompositions={"A1": draft(), "A2": draft()})
        root = TaskRecord(id="root", role="analysis", target_version="T1", objective="Analyze")
        state["tasks"].update({identifier: replace(root, id=identifier, parent_task_id="root") for identifier in ("W1", "W2")})
        relation = SourcedStatement(text="Right covers left", source_refs=(SourceRef(result_id="A1", item_id="R2"),))
        sourced = replace(summary, relationships=(relation,))
        result = ResultRecord(id="final", task_id="root", status="completed", summary="Summary", analysis_detail=sourced)
        validate_analysis_result(state, result, root)
        with self.assertRaisesRegex(ValueError, "Invalid observation reference"):
            validate_analysis_result(state, replace(result, analysis_detail=replace(sourced, key_observations=(relation,))), root)
        draft_source = replace(relation, source_refs=(SourceRef(result_id="A1", item_id="R1"),))
        with self.assertRaisesRegex(ValueError, "Invalid item reference"):
            validate_analysis_result(state, replace(result, analysis_detail=replace(sourced, relationships=(draft_source,))), root)
        with self.assertRaisesRegex(ValueError, "mapping source"):
            validate_summary_visual(
                state, replace(summary, visual_mappings=(replace(summary.visual_mappings[0], source_result_id="A2", source_id="unknown"),))
            )
        invalid_mappings = (
            replace(summary.visual_mappings[0], target_id="RL1"),
            replace(summary.visual_mappings[1], source_id="O1"),
        )
        with self.assertRaises(ValueError) as caught:
            validate_summary_visual(state, replace(summary, visual_mappings=invalid_mappings))
        self.assertIn("visual_mappings[0]: Invalid mapping target references: ('RL1',)", str(caught.exception))
        self.assertIn("visual_mappings[1]: Invalid mapping source references: ('O1',)", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "hypothesis IDs"):
            validate_summary_visual(state, replace(summary, hypotheses=(summary.hypotheses[0],) * 2))
