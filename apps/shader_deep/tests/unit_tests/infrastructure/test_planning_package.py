"""规划输入固定、正式包发布、身份与可迁移读取."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.scene_observations import ObservationIssue, SceneObservations
from shader_deep.domain.scene_planning import ScenePlanningSubmission
from shader_deep.infrastructure.storage.observation_package import ObservationManifest, ObservationReference, write_observation_package
from shader_deep.infrastructure.storage.planning_inputs import capture_planning_source, content_sha256, parse_json
from shader_deep.infrastructure.storage.planning_package import (
    PAYLOAD_FILES,
    PLANNING_FILES,
    read_planning_package,
    read_planning_sketch,
    write_planning_package,
)
from tests.unit_tests.domain.test_scene_planning import planning_observations, planning_submission

if TYPE_CHECKING:
    from shader_deep.infrastructure.storage.planning_inputs import CapturedPlanningSource


class PlanningPackageTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        output = io.BytesIO()
        Image.new("RGB", (32, 18), "white").save(output, format="PNG")
        self.reference = output.getvalue()
        self.observations = planning_observations()
        self.report = planning_submission()
        self.observation_dir = self.publish_observation("observation")

    def publish_observation(self, name: str, *, gaps: tuple[ObservationIssue, ...] = ()) -> Path:
        manifest = ObservationManifest(
            reference=ObservationReference(sha256=hashlib.sha256(self.reference).hexdigest()),
            status="partial" if gaps else "completed",
            context_notes=("浅灰背景",),
            gaps=gaps,
        )
        return write_observation_package(self.root / name, self.observations, manifest, self.reference)

    def capture(self, name: str = "run", *, options: GenerationOptions | None = None) -> CapturedPlanningSource:
        return capture_planning_source(self.observation_dir, self.root / name, "复刻整图并保留交叠变化", generation_options=options)

    def publish(self) -> Path:
        return write_planning_package(self.root / "planning", self.capture(), self.report)

    def reseal(self, directory: Path) -> None:
        path = directory / "manifest.json"
        manifest = json.loads(path.read_bytes())
        contents = {name: (directory / name).read_bytes() for name in PAYLOAD_FILES}
        manifest["files"] = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
        manifest["content_sha256"] = content_sha256(contents)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_official_roundtrip_relocation_and_explicit_alternative(self) -> None:
        source = self.capture(options=GenerationOptions(width=640, height=360, time=2.5))
        self.assertEqual(source.directory, self.root / "run")
        report = write_planning_package(self.root / "planning", source, self.report)
        package = read_planning_package(report)
        self.assertEqual(package.report, self.report)
        self.assertEqual(package.source.constraints.time, 2.5)
        self.assertEqual(package.source.constraints.width, 640)
        self.assertEqual(package.manifest.status, "completed")
        self.assertEqual(package.manifest.observation_sha256, source.observation_sha256)
        self.assertEqual(set(PLANNING_FILES), {"manifest.json", *package.manifest.files})
        moved = self.root / "moved"
        report.rename(moved)
        loaded = read_planning_package(moved)
        self.assertEqual(loaded.content_sha256, package.content_sha256)
        self.assertEqual(loaded.source.reference_path, moved / "observation/reference.png")
        self.assertEqual(read_planning_sketch(moved, "S1", alternative_index=0).choices["R2"], ("M4", "M2"))

    def test_captured_input_preserves_source_after_original_changes(self) -> None:
        source = self.capture()
        self.assertEqual((source.constraints.width, source.constraints.height), (32, 18))
        (self.observation_dir / "features.json").write_text("[]", encoding="utf-8")
        package = read_planning_package(write_planning_package(self.root / "planning", source, self.report))
        self.assertEqual(package.source.observations, self.observations)
        self.assertEqual(package.source.reference_bytes, self.reference)

    def test_all_observation_business_fields_change_bound_identity(self) -> None:
        original = self.capture("original").observation_sha256
        changes = (
            ("features.json", lambda value: value[0].update(description="新的轮廓描述")),
            ("manifest.json", lambda value: value.update(context_notes=["新的整图上下文"])),
            ("manifest.json", lambda value: value.update(open_questions=[{"refs": ["E2"], "description": "前圆需复核"}])),
            ("manifest.json", lambda value: value.update(status="partial", gaps=[{"refs": [], "description": "必要观察未完成"}])),
        )
        for offset, (name, change) in enumerate(changes):
            path = self.observation_dir / name
            previous = path.read_bytes()
            value = json.loads(previous)
            change(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(name=name, offset=offset):
                self.assertNotEqual(self.capture(f"changed-{offset}").observation_sha256, original)
            path.write_bytes(previous)

    def test_upper_element_and_global_gaps_are_kept_in_selected_material(self) -> None:
        for offset, refs in enumerate(((), ("E2",), ("R2",))):
            gap = ObservationIssue(refs=refs, description="上游必要观察缺失")
            self.observation_dir = self.publish_observation(f"partial-observation-{offset}", gaps=(gap,))
            report = write_planning_package(self.root / f"planning-{offset}", self.capture(f"run-{offset}"), self.report)
            package = read_planning_package(report)
            selected = read_planning_sketch(report, "S1")
            with self.subTest(refs=refs):
                self.assertEqual(package.manifest.status, "partial")
                self.assertEqual(selected.observation_gaps, (gap,))
                self.assertFalse(selected.complete)
                self.assertEqual(selected.gaps, ())

    def test_multiple_sketches_preserve_per_sketch_completeness(self) -> None:
        payload = self.report.model_dump(mode="json")
        another = {**payload["sketches"][0], "id": "S2", "gaps": [{"refs": ["F3"], "description": "前圆参数来源缺失"}]}
        payload["sketches"].append(another)
        report = write_planning_package(self.root / "planning", self.capture(), ScenePlanningSubmission.model_validate(payload))
        manifest = read_planning_package(report).manifest
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.sketch_status, {"S1": "completed", "S2": "partial"})
        self.assertFalse(read_planning_sketch(report, "S2").complete)

    def test_pure_background_with_empty_mechanisms_is_readable(self) -> None:
        self.observations = SceneObservations(elements=(), features=(), relations=())
        self.observation_dir = self.publish_observation("background")
        self.report = ScenePlanningSubmission.model_validate(
            {"mechanisms": [], "sketches": [{"id": "S1", "name": "纯背景", "default": {}, "composition": "输出浅灰色。"}]}
        )
        package = read_planning_package(self.publish())
        self.assertEqual(package.manifest.status, "completed")
        self.assertEqual(read_planning_sketch(package.directory, "S1").mechanisms, ())

    def test_existing_destination_and_failed_publication_preserve_inputs(self) -> None:
        report = self.publish()
        before = (report / "manifest.json").read_bytes()
        with self.assertRaises(ValueError):
            write_planning_package(report, self.capture("another-run"), self.report)
        self.assertEqual((report / "manifest.json").read_bytes(), before)
        source = self.capture("failure-run")
        for target in ("_write_contents", "read_planning_package"):
            with (
                self.subTest(target=target),
                patch(f"shader_deep.infrastructure.storage.planning_package.{target}", side_effect=OSError("disk failure")),
                self.assertRaises(OSError),
            ):
                write_planning_package(self.root / "failed-publication", source, self.report)
            self.assertFalse((self.root / "failed-publication").exists())
            self.assertEqual(list(self.root.glob(".failed-publication-*")), [])

    def test_tampering_missing_files_and_symlink_escape_are_rejected(self) -> None:
        report = self.publish()
        for name in ("mechanisms.json", "observation/features.json", "generation_constraints.json"):
            path = report / name
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "哈希"):
                read_planning_package(report)
            path.write_bytes(before)
        path = report / "observation/reference.png"
        before = path.read_bytes()
        path.unlink()
        with self.assertRaises(OSError):
            read_planning_package(report)
        outside = self.root / "outside.png"
        outside.write_bytes(before)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "路径"):
            read_planning_package(report)

    def test_derived_candidates_and_status_are_recomputed(self) -> None:
        report = self.publish()
        path = report / "candidates.json"
        original = path.read_bytes()
        path.write_text("[]", encoding="utf-8")
        self.reseal(report)
        with self.assertRaisesRegex(ValueError, "候选索引"):
            read_planning_package(report)
        path.write_bytes(original)
        self.reseal(report)
        manifest_path = report / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["sketch_status"]["S1"] = "partial"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "完整性"):
            read_planning_package(report)

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self) -> None:
        for content in (b'{"F1": ["M1"], "F1": ["M2"]}', b'{"time": NaN}', b'{"time": Infinity}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_json(content)
        report = self.publish()
        path = report / "sketches.json"
        contents = path.read_text(encoding="utf-8").replace('"F1": [', '"F1": ["M1"], "F1": [', 1)
        path.write_text(contents, encoding="utf-8")
        self.reseal(report)
        with self.assertRaisesRegex(ValueError, "对象键重复"):
            read_planning_package(report)

    def test_protocol_file_names_and_bound_source_changes_are_rejected(self) -> None:
        report = self.publish()
        path = report / "manifest.json"
        original = path.read_bytes()
        value = json.loads(original)
        value["files"]["../escape"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "必需文件"):
            read_planning_package(report)
        source = self.capture("changed-run")
        (source.observation_dir / "features.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "运行期间改变"):
            write_planning_package(self.root / "changed-source", source, self.report)
