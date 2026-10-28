"""原生观察工具的呈现隔离、证据交接与引用保义合同."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase

from PIL import Image

from shader_deep.agents.analysis.observations import ReferenceObservations
from shader_deep.domain.evidence import ImageRegion, MeasurementRequest, MeasurementSpec
from shader_deep.domain.five_libraries import Element, ExplorationSubmission, Feature, Mechanism, MechanismRef, Sketch, validate_exploration
from shader_deep.domain.five_libraries.models import MergeGroup, MergeProposal
from shader_deep.domain.five_libraries.operations import apply_merges, collect_explorations, exploration_mappings
from shader_deep.domain.five_libraries.references import parse_references
from shader_deep.domain.five_libraries.validation import exploration_library
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.storage.analysis_materials import package_materials
from shader_deep.infrastructure.storage.generation_inputs import _reuse_generation_inputs, capture_generation_inputs
from shader_deep.infrastructure.storage.report_evidence import EvidenceAsset, evidence_bytes, evidence_path
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, read_report_package, read_sketch, write_report_package
from shader_deep.workflows.analysis.entry import _delivery
from shader_deep.workflows.analysis.session import AnalysisSession
from shader_deep.workflows.options import AnalysisOptions


class ReferenceObservationTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.target = Element(id="E1", name="目标", region="中央", feature_ids=())
        self.feature = Feature(id="F1", description="圆形", mechanism_refs=(MechanismRef(id="M1"),))
        self.method = Mechanism(id="M1", name="轮廓", method="基础圆盘平滑遮罩, 可调半径和边缘")
        self.sketch = Sketch(id="S1", element_id="E1", name="圆盘", default={"F1": ("M1",)}, composition="用 [[M1]] 保持 [[F1]]")
        with Image.new("RGBA", (20, 16), (10, 20, 30, 128)) as image:
            stream = io.BytesIO()
            image.save(stream, format="PNG")
        self.reference = stream.getvalue()
        self.url = "data:image/png;base64," + base64.b64encode(self.reference).decode()
        self.active = True

    def _evidence(self) -> ReferenceObservations:
        return ReferenceObservations(
            self.url,
            self.root / "observations",
            "explorer-1",
            2,
            target_box=ImageRegion(left=2, top=3, right=10, bottom=12),
            active=lambda: self.active,
        )

    def test_variables_and_explicit_references_have_different_meanings(self) -> None:
        self.assertEqual(parse_references("R0 与 M2 是参数", {"F1": "features"}, field="method"), ())
        with self.assertRaises(ValueError):
            parse_references("F1", {"F1": "features"}, field="description")
        with self.assertRaises(ValueError):
            parse_references("[[R0]]", {"F1": "features"}, field="description")

    def test_focus_measurement_presentation_and_isolation(self) -> None:
        evidence = self._evidence()
        self.assertEqual(evidence.focus["offset"], [1, 2])
        with self.assertRaisesRegex(ValueError, "超出"):
            ReferenceObservations(self.url, self.root / "invalid", "worker", 1, target_box=ImageRegion(left=0, top=0, right=21, bottom=10))
        tool = evidence.tools()[0]
        response = json.loads(tool.invoke({"kind": "crop", "region": {"left": 2, "top": 3, "right": 10, "bottom": 12}, "question": "边界是否平滑"}))
        identity = response["record"]["id"]
        report = ExplorationSubmission(
            features=(self.feature,), relations=(), mechanisms=(self.method,), sketches=(self.sketch,), evidence_links={"F1": (identity,)}
        )
        with self.assertRaisesRegex(ValueError, "后续请求"):
            evidence.validate(report)
        message = evidence.context(_message({}, self.url))
        images = [item for item in message.content if isinstance(item, dict) and item.get("type") == "image_url"]
        self.assertEqual(len(images), 3)
        evidence.mark_presented([message])
        evidence.validate(report)
        with Image.open(io.BytesIO(base64.b64decode(images[-1]["image_url"]["url"].split(",")[1]))) as crop:
            self.assertEqual(crop.size, (8, 9))
            self.assertEqual(crop.getpixel((0, 0)), (10, 20, 30, 128))
        with self.assertRaises(ValueError):
            evidence.validate(report.model_copy(update={"evidence_links": {"F1": ("other-task-e001",)}}))
        # 同规格复用, 不重复扣预算.
        tool.invoke({"kind": "crop", "region": {"left": 2, "top": 3, "right": 10, "bottom": 12}, "question": "复查"})
        self.assertEqual(len(evidence.measurements.records), 1)
        self.active = False
        with self.assertRaises(ValueError):
            tool.invoke({"kind": "region_stats", "region": {"left": 0, "top": 0, "right": 1, "bottom": 1}, "question": "颜色"})

    def test_evidence_package_moves_and_generation_freezes_all_bytes(self) -> None:
        evidence = self._evidence()
        record, _ = evidence.measurements.measure(
            MeasurementRequest(question="轮廓", measurement=MeasurementSpec(kind="crop", region=ImageRegion(left=2, top=3, right=10, bottom=12)))
        )
        evidence.mark_presented([evidence.context(_message({}, self.url))])
        report = ExplorationSubmission(
            features=(self.feature,),
            relations=(),
            mechanisms=(self.method,),
            sketches=(self.sketch,),
            context_notes=("辅助背景",),
            evidence_links={"F1": (record.id,)},
        )
        materials = evidence.snapshot(report)
        packed, assets = package_materials(materials, hashlib.sha256(self.reference).hexdigest())
        declaration = EvidenceAsset(path="evidence.json", sha256=hashlib.sha256(evidence_bytes(packed)).hexdigest())

        library = exploration_library(report, self.target)
        manifest = ReportManifest(
            reference=ReferenceAsset(sha256=hashlib.sha256(self.reference).hexdigest()),
            target_element_id="E1",
            status="completed",
            evidence=declaration,
        )
        package = write_report_package(self.root / "report", library, manifest, self.reference, evidence=packed, evidence_assets=assets)
        moved = self.root / "moved"
        shutil.move(package, moved)
        read_report_package(moved)
        self.assertEqual(read_sketch(moved, "S1")["evidence"]["links"], {"F1": [record.id]})
        captured = capture_generation_inputs(moved, self.root / "generation", "S1")
        copied = _reuse_generation_inputs(captured, self.root / "generation-2", "S1")
        self.assertEqual(captured.binding.content_sha256, copied.binding.content_sha256)
        self.assertTrue(any(path.endswith("evidence.json") for path in captured.files))
        asset = next(iter(assets))
        (moved / asset).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "哈希"):
            read_report_package(moved)
        read_report_package(Path(captured.binding.report_path))
        (Path(copied.binding.report_path) / "evidence.json").unlink()
        with self.assertRaises(OSError):
            read_report_package(Path(copied.binding.report_path))

    def test_namespace_allocation_preserves_unregistered_variables_across_sources(self) -> None:
        first = ExplorationSubmission(
            features=(self.feature,),
            relations=(),
            mechanisms=(self.method.model_copy(update={"method": "参数 M2 控制半径"}),),
            sketches=(self.sketch,),
        )
        second = first.model_copy(update={"mechanisms": (self.method,)})
        validate_exploration(first, self.target)
        library, _, _ = collect_explorations(self.target, {"one": first, "two": second})
        mapping = exploration_mappings(self.target, {"one": first, "two": second})
        self.assertEqual(mapping["one"]["M1"], "M1")
        self.assertEqual(mapping["two"]["M1"], "M3")
        self.assertEqual(library.mechanisms[0].method, "参数 M2 控制半径")
        self.assertEqual(library.sketches[1].default, {"F2": ("M3",)})

        merged = apply_merges(library, MergeProposal(groups=(MergeGroup(kind="mechanisms", ids=("M1", "M3"), keep="M1"),)), "E1")
        manifest = ReportManifest(
            reference=ReferenceAsset(sha256=hashlib.sha256(self.reference).hexdigest()), target_element_id="E1", status="completed"
        )
        package = write_report_package(self.root / "variable-report", merged, manifest, self.reference)
        self.assertEqual(read_report_package(package)[1].mechanisms[0].method, "参数 M2 控制半径")

    def test_evidence_paths_reject_escape_and_external_symlink(self) -> None:
        for path in ("/outside/secret.png", "evidence/../secret.png", "other/secret.png", "evidence/secret.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                evidence_path(self.root, path)
        (self.root / "evidence").mkdir()
        outside = self.root.parent / (self.root.name + "-external.png")
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_bytes(b"not allowed")
        (self.root / "evidence" / "link.png").symlink_to(outside)
        with self.assertRaises(ValueError):
            evidence_path(self.root, "evidence/link.png")

    def test_native_merge_deduplicates_evidence_and_keeps_delivery_bytes_private(self) -> None:
        evidence = self._evidence()
        record, _ = evidence.measurements.measure(
            MeasurementRequest(question="轮廓", measurement=MeasurementSpec(kind="crop", region=ImageRegion(left=2, top=3, right=10, bottom=12)))
        )
        evidence.mark_presented([evidence.context(_message({}, self.url))])
        value = ExplorationSubmission(
            features=(self.feature, self.feature.model_copy(update={"id": "F2"})),
            relations=(),
            mechanisms=(self.method,),
            sketches=(),
            context_notes=("背景仅作上下文",),
            evidence_links={"F1": (record.id,), "F2": (record.id,)},
        )
        session = AnalysisSession("分析主体", self.url, self.root / "native", AnalysisOptions(max_tasks=2))
        session.submit_elements((self.target,))
        session.freeze_plan("E1", ("轮廓", "颜色"))
        session.admit_task("explorer-1")
        session.submit_exploration("explorer-1", value, materials=evidence.snapshot(value))
        session.fail_task("explorer-2", "未完成")
        session.admit_task("integrator")
        session.submit_integration(MergeProposal(groups=(MergeGroup(kind="features", ids=("F1", "F2"), keep="F1"),)))
        selected = session.versions["V1"]["materials"]
        self.assertEqual(selected["links"], {"F1": [record.id]})
        self.assertIn("asset_contents", selected)
        self.assertNotIn("asset_contents", str(session.read_analysis_result("V1")))
        shutil.rmtree(evidence.measurements.directory)
        result = session.finalize()
        self.assertEqual((result.status, result.selected_version), ("partial", "V1"))
        _delivery(session.directory, self.reference, result, session)
        manifest, library = read_report_package(session.directory / "report")
        self.assertIsNotNone(manifest.evidence)
        self.assertEqual(len(library.features), 1)
        packed = json.loads((session.directory / "report" / "evidence.json").read_text())
        self.assertEqual(packed["links"], {"F1": [record.id]})
        self.assertEqual(len(packed["assets"]), 1)
