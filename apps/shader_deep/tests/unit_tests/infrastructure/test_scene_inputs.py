"""整图输入的来源身份、同图约束及固定材料行为."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from shader_deep.domain.scene import SceneElementSource, ScenePlan
from shader_deep.infrastructure.storage.generation_inputs import capture_generation_inputs
from shader_deep.infrastructure.storage.scene_inputs import capture_scene_inputs, scene_input_records
from tests.unit_tests.fixtures._scene_fixture import CIRCLE_CODE, RECTANGLE_CODE, scene_sources


def source_record(source: SceneElementSource) -> dict[str, object]:
    return json.loads((Path(source.run_dir) / "run.json").read_text())


def different_reference(source: SceneElementSource, root: Path) -> None:
    record = source_record(source)
    task = record["blackboard"]["tasks"][record["task_id"]]
    report = Path(task["generation_binding"]["report_path"])
    with Image.new("RGB", (32, 24), "blue") as image:
        image.save(report / "reference.png")
    manifest = json.loads((report / "manifest.json").read_text())
    manifest["reference"]["sha256"] = hashlib.sha256((report / "reference.png").read_bytes()).hexdigest()
    (report / "manifest.json").write_text(json.dumps(manifest))
    captured = capture_generation_inputs(report, root / "different-reference", "S1")
    task["generation_binding"] = asdict(captured.binding)
    record["blackboard"]["targets"][task["target_version"]]["reference_path"] = captured.reference_path
    (Path(source.run_dir) / "run.json").write_text(json.dumps(record))


def same_element_alternative(source: SceneElementSource, root: Path) -> SceneElementSource:
    directory = root / "same-element-alternative"
    shutil.copytree(source.run_dir, directory)
    record = source_record(source)
    record["blackboard"]["tasks"][record["task_id"]]["generation_binding"]["alternative"] = 0
    (directory / "run.json").write_text(json.dumps(record))
    return replace(source, run_dir=str(directory))


class SceneInputsTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def plan(self, sources: tuple[SceneElementSource, ...]) -> ScenePlan:
        return ScenePlan(request="组合圆形与方形", background="深灰", layout="保持原图布局与遮挡", elements=sources)

    def test_same_image_different_reports_allow_shared_short_ids_and_freeze_materials(self) -> None:
        sources = scene_sources(self.root / "valid")
        source_paths = [Path(source.run_dir) / name for source in sources for name in ("run.json", "selected.glsl", "selected.png")]
        before_sources = {path: path.read_bytes() for path in source_paths}
        captured = capture_scene_inputs(self.plan(sources), self.root / "scene")
        self.assertEqual({element.inputs.binding.element_id for element in captured.elements}, {"E1"})
        self.assertEqual({element.source.candidate_id for element in captured.elements}, {"C1"})
        self.assertEqual(len({element.inputs.binding.content_sha256 for element in captured.elements}), 2)
        self.assertEqual((captured.width, captured.height), (32, 24))
        self.assertEqual([element.code for element in captured.elements], [CIRCLE_CODE, RECTANGLE_CODE])
        self.assertEqual(base64.b64decode(captured.reference_url.split(",", 1)[1]), Path(captured.reference_path).read_bytes())
        self.assertEqual({path: path.read_bytes() for path in source_paths}, before_sources)
        before_copies = {path: Path(path).read_bytes() for path in captured.files}
        for source in sources:
            (Path(source.run_dir) / "selected.glsl").write_text("changed source")
            (Path(source.run_dir) / "selected.png").unlink()
        self.assertEqual([element.code for element in captured.elements], [CIRCLE_CODE, RECTANGLE_CODE])
        self.assertEqual({path: Path(path).read_bytes() for path in captured.files}, before_copies)
        mappings = scene_input_records(captured)
        self.assertEqual([item["source"] for item in mappings], [asdict(source) for source in captured.plan.elements])
        self.assertNotIn("code", mappings[0])
        self.assertNotIn("preview_url", mappings[0])
        self.assertTrue(all(Path(element.source.run_dir).is_absolute() for element in captured.elements))

    def test_invalid_selection_assets_reference_and_duplicate_element_are_rejected(self) -> None:
        for case in ("selection", "code", "reference", "element"):
            with self.subTest(case=case):
                root = self.root / case
                sources = scene_sources(root)
                if case == "selection":
                    sources = (replace(sources[0], candidate_id="missing"), sources[1])
                elif case == "code":
                    (Path(sources[0].run_dir) / "selected.glsl").unlink()
                elif case == "reference":
                    different_reference(sources[1], root)
                else:
                    sources = (sources[0], same_element_alternative(sources[0], root))
                with self.assertRaises((ValueError, OSError)):
                    capture_scene_inputs(self.plan(sources), root / "scene")

    def test_changed_report_body_cannot_keep_the_original_binding_identity(self) -> None:
        sources = scene_sources(self.root / "changed")
        record = source_record(sources[0])
        binding = record["blackboard"]["tasks"][record["task_id"]]["generation_binding"]
        mechanisms_path = Path(binding["report_path"]) / "mechanisms.json"
        mechanisms = json.loads(mechanisms_path.read_text())
        mechanisms[0]["method"] = "修改来源的实现方法"
        mechanisms_path.write_text(json.dumps(mechanisms))
        with self.assertRaisesRegex(ValueError, "generation binding"):
            capture_scene_inputs(self.plan(sources), self.root / "scene")
