"""固定生成输入的选择语义、内容身份与文件独立性."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.five_libraries import Alternative, Issue, Mechanism, MechanismRef, Sketch
from shader_deep.domain.tasks import CandidateRecord, TargetRecord, TaskRecord
from shader_deep.infrastructure.storage.generation_inputs import capture_generation_inputs
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from tests.unit_tests.infrastructure.test_report_package import package_libraries


class GenerationInputsTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with io.BytesIO() as buffer, Image.new("RGB", (32, 24), "pink") as image:
            image.save(buffer, format="PNG")
            self.reference = buffer.getvalue()
        library = package_libraries()
        feature = library.features[1].model_copy(update={"description": "右下亮斑", "mechanism_refs": (MechanismRef(id="M1"), MechanismRef(id="M3"))})
        sketch = library.sketches[1].model_copy(update={"alternatives": (Alternative(choices={"F2": ("M3",)}, reason="试验辅助坐标"),)})
        self.library = library.model_copy(
            update={
                "features": (library.features[0], feature),
                "mechanisms": (*library.mechanisms, Mechanism(id="M3", name="辅助观察", method="使用共享坐标")),
                "sketches": (library.sketches[0], sketch),
            }
        )
        self.manifest = ReportManifest(
            reference=ReferenceAsset(sha256=hashlib.sha256(self.reference).hexdigest()),
            target_element_id="E1",
            status="partial",
            gaps=(Issue(refs=(), description="一个探索方向未完成"),),
            open_questions=(Issue(refs=("S2",), description="需核对右下亮斑"),),
        )
        self.report = write_report_package(self.root / "report", self.library, self.manifest, self.reference)

    def test_default_and_alternative_keep_public_references_separate(self) -> None:
        default = capture_generation_inputs(self.report, self.root / "default", "S1")
        alternate = capture_generation_inputs(self.report, self.root / "alternate", "S1", alternative=0)
        for captured, mechanism in ((default, "M1"), (alternate, "M2")):
            with self.subTest(mechanism=mechanism):
                scheme = json.loads(captured.scheme_json)
                self.assertEqual(scheme["selected"]["choices"], {"F1": [mechanism]})
                self.assertEqual([item["id"] for item in scheme["reference_content"]["mechanisms"]], [mechanism])
                self.assertEqual([item["id"] for item in scheme["issue_references"]["mechanisms"]], ["M1", "M3"])
                self.assertEqual([item["id"] for item in scheme["issue_references"]["features"]], ["F2"])
                self.assertEqual(scheme["issue_references"]["sketches"][0]["alternatives"][0]["choices"], {"F2": ["M3"]})
                self.assertEqual(scheme["manifest"]["gaps"][0]["description"], "一个探索方向未完成")
                self.assertNotIn("default", scheme)
                self.assertNotIn("alternatives", scheme)
                self.assertEqual((captured.width, captured.height), (32, 24))
                self.assertEqual(base64.b64decode(captured.reference_url.split(",", 1)[1]), self.reference)
        self.assertEqual(default.binding.content_sha256, alternate.binding.content_sha256)
        self.assertEqual(json.loads(alternate.scheme_json)["selected_alternative"]["reason"], "保留亮斑形态的不确定性")

    def test_nested_sketch_references_supply_bodies_without_adopting_their_choices(self) -> None:
        feature = self.library.features[1].model_copy(
            update={"description": "右下亮斑可参考 [[S3]]", "mechanism_refs": (*self.library.features[1].mechanism_refs, MechanismRef(id="M4"))}
        )
        selected = self.library.sketches[0].model_copy(update={"composition": "对 [[F1]] 应用局部遮罩, 参考 [[S2]] 的组织"})
        referenced = self.library.sketches[1].model_copy(update={"composition": "按原图位置组织遮罩"})
        nested = Sketch(id="S3", element_id="E1", name="嵌套参考", default={"F2": ("M4",)}, composition="对 [[F2]] 校正, 再核对 [[S2]]")
        library = self.library.model_copy(
            update={
                "features": (self.library.features[0], feature),
                "mechanisms": (*self.library.mechanisms, Mechanism(id="M4", name="局部校正", method="使用局部距离场")),
                "sketches": (selected, referenced, nested),
            }
        )
        report = write_report_package(self.root / "nested-report", library, self.manifest, self.reference)
        captured = capture_generation_inputs(report, self.root / "nested-run", "S1", alternative=0)
        scheme = json.loads(captured.scheme_json)
        self.assertEqual(scheme["selected"]["choices"], {"F1": ["M2"]})
        for key, mechanisms in (("reference_content", ["M1", "M2", "M3", "M4"]), ("issue_references", ["M1", "M3", "M4"])):
            self.assertEqual([item["id"] for item in scheme[key]["sketches"]], ["S2", "S3"])
            self.assertEqual([item["id"] for item in scheme[key]["mechanisms"]], mechanisms)
            self.assertEqual(next(item for item in scheme[key]["mechanisms"] if item["id"] == "M4")["method"], "使用局部距离场")

    def test_source_changes_do_not_change_report_or_baseline_materials(self) -> None:
        code_path, preview_path = self.root / "old.glsl", self.root / "old.png"
        code_path.write_text("void mainImage() {}", encoding="utf-8")
        preview_path.write_bytes(self.reference)
        baseline = CandidateRecord(id="B1", task_id="G0", code_path="old.glsl", preview_path="old.png")
        captured = capture_generation_inputs(self.report, self.root / "fixed", "S1", baseline=baseline, asset_root=self.root)
        before = {path: Path(path).read_bytes() for path in captured.files}
        mechanisms = json.loads((self.report / "mechanisms.json").read_text())
        mechanisms[0]["method"] = "改变了实现方法"
        (self.report / "mechanisms.json").write_text(json.dumps(mechanisms), encoding="utf-8")
        newer = capture_generation_inputs(self.report, self.root / "newer", "S1")
        code_path.unlink()
        preview_path.unlink()
        (self.report / "reference.png").unlink()
        self.assertNotEqual(captured.binding.content_sha256, newer.binding.content_sha256)
        self.assertNotIn("改变了实现方法", captured.scheme_json)
        self.assertEqual({path: Path(path).read_bytes() for path in captured.files}, before)
        self.assertIsNotNone(captured.baseline)
        self.assertEqual(captured.baseline.code, "void mainImage() {}")
        self.assertEqual(base64.b64decode(captured.baseline.preview_url.split(",", 1)[1]), self.reference)
        self.assertEqual(baseline.code_path, "old.glsl")
        self.assertEqual(baseline.preview_path, "old.png")

    def test_bad_selection_and_missing_required_assets_fail_preparation(self) -> None:
        with self.assertRaises(ValueError):
            capture_generation_inputs(self.report, self.root / "bad-selection", "S1", alternative=10)
        code = self.root / "base.glsl"
        code.write_text("void mainImage() {}", encoding="utf-8")
        baseline = CandidateRecord(id="B1", task_id="G0", code_path=str(code))
        captured = capture_generation_inputs(self.report, self.root / "no-preview", "S1", baseline=baseline)
        self.assertIsNone(captured.baseline.preview_url)
        with self.assertRaises(OSError):
            capture_generation_inputs(
                self.report, self.root / "missing-preview", "S1", baseline=replace(baseline, preview_path=str(self.root / "missing.png"))
            )
        preview = self.root / "wrong-extension.jpg"
        preview.write_bytes(self.reference)
        with self.assertRaises(ValueError):
            capture_generation_inputs(self.report, self.root / "wrong-preview", "S1", baseline=replace(baseline, preview_path=str(preview)))

    def test_optional_binding_preserves_old_tasks_and_rejects_wrong_roles(self) -> None:
        captured = capture_generation_inputs(self.report, self.root / "binding", "S1")
        state = add_target(new_blackboard(), TargetRecord(version="T1", request="重建", reference_path=captured.reference_path))
        old = TaskRecord(id="old", role="generation", target_version="T1", objective="旧任务")
        state = add_task(state, old)
        bound = replace(old, id="new", generation_binding=captured.binding)
        state = add_task(state, bound)
        self.assertIsNone(state["tasks"]["old"].generation_binding)
        self.assertEqual(asdict(state["tasks"]["new"])["generation_binding"]["sketch_id"], "S1")
        for invalid in (
            replace(bound, id="analysis", role="analysis"),
            replace(bound, id="invalid", generation_binding=replace(captured.binding, content_sha256="bad")),
        ):
            with self.subTest(task=invalid.id), self.assertRaises(ValueError):
                add_task(state, invalid)
