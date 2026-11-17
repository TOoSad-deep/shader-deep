"""观察包的发布、回读、失败清理与路径和引用完整性."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep.domain.scene_observations import ObservationIssue, SceneObservationSubmission, build_scene_observations
from shader_deep.infrastructure.storage.observation_package import (
    ObservationManifest,
    ObservationReference,
    read_observation_package,
    write_observation_package,
)
from tests.unit_tests.domain.test_scene_observations import scene_submission


class ObservationPackageTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = b"fixed-reference-bytes"
        self.submission = scene_submission()
        self.scene = build_scene_observations(self.submission)
        self.manifest = ObservationManifest(
            reference=ObservationReference(sha256=hashlib.sha256(self.reference).hexdigest()),
            status="completed",
            context_notes=self.submission.context_notes,
            open_questions=self.submission.open_questions,
        )

    def publish(self) -> Path:
        return write_observation_package(self.root / "report", self.scene, self.manifest, self.reference)

    def test_roundtrip_and_relocation_preserve_all_scene_semantics(self) -> None:
        report = self.publish()
        moved = self.root / "moved"
        report.rename(moved)
        self.assertEqual(read_observation_package(moved), (self.manifest, self.scene))
        self.assertEqual(
            {item.name for item in moved.iterdir()},
            {
                "manifest.json",
                "reference.png",
                "elements.json",
                "features.json",
                "relations.json",
                "README.md",
            },
        )
        entry = (moved / "README.md").read_text()
        self.assertIn("- E2 后方圆", entry)
        self.assertIn("  - F8 青蓝色场", entry)
        self.assertIn("- R9 前方覆盖后方", entry)
        self.assertIn("未知项 (R9)", entry)
        elements = json.loads((moved / "elements.json").read_text())
        self.assertEqual(elements[0]["feature_ids"], ["F8", "F3"])
        self.assertNotIn("features", elements[0])

    def test_background_only_package_has_no_fabricated_elements(self) -> None:
        submission = SceneObservationSubmission(elements=(), context_notes=("白色背景",))
        self.scene = build_scene_observations(submission)
        self.manifest = self.manifest.model_copy(update={"context_notes": submission.context_notes, "open_questions": ()})
        self.assertEqual(read_observation_package(self.publish())[1].elements, ())

    def test_partial_requires_gaps_and_keeps_unknown_observations_separate(self) -> None:
        issue = ObservationIssue(refs=("E7",), description="无法辨认必要边缘细节")
        self.manifest = self.manifest.model_copy(update={"status": "partial", "gaps": (issue,)})
        report = self.publish()
        self.assertEqual(read_observation_package(report)[0].gaps, (issue,))
        self.assertIn("必要观察缺口 (E7)", (report / "README.md").read_text())

    def test_invalid_status_issues_hash_and_copied_protocol_never_publish(self) -> None:
        invalid = (
            self.manifest.model_copy(update={"status": "partial"}),
            self.manifest.model_copy(update={"gaps": (ObservationIssue(description="缺失"),)}),
            self.manifest.model_copy(update={"open_questions": (ObservationIssue(refs=("E99",), description="未知"),)}),
            self.manifest.model_copy(update={"reference": ObservationReference(sha256="0" * 64)}),
            self.manifest.model_copy(update={"schema_version": "five_libraries_v1"}),
        )
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                write_observation_package(self.root / "report", self.scene, manifest, self.reference)
        self.assertFalse((self.root / "report").exists())

    def test_existing_directory_is_not_overwritten(self) -> None:
        report = self.publish()
        before = {item.name: item.read_bytes() for item in report.iterdir()}
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual({item.name: item.read_bytes() for item in report.iterdir()}, before)

    def test_write_or_official_read_failure_cleans_staging_without_publication(self) -> None:
        for name in ("_write_contents", "read_observation_package"):
            with (
                self.subTest(name=name),
                patch(f"shader_deep.infrastructure.storage.observation_package.{name}", side_effect=OSError("disk failure")),
                self.assertRaises(OSError),
            ):
                self.publish()
            self.assertEqual(list(self.root.iterdir()), [])

    def test_tampered_reference_and_structural_links_are_rejected_on_read(self) -> None:
        report = self.publish()
        reference = report / "reference.png"
        reference.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            read_observation_package(report)
        reference.write_bytes(self.reference)
        features = json.loads((report / "features.json").read_text())
        features.append({"id": "F99", "description": "孤立特征"})
        (report / "features.json").write_text(json.dumps(features))
        with self.assertRaises(ValueError):
            read_observation_package(report)

    def test_symlink_escape_is_rejected_for_reference_and_json_assets(self) -> None:
        report = self.publish()
        for name in ("reference.png", "features.json"):
            path = report / name
            original = path.read_bytes()
            outside = self.root / name
            outside.write_bytes(original)
            path.unlink()
            path.symlink_to(outside)
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_observation_package(report)
            path.unlink()
            path.write_bytes(original)

    def test_unknown_protocol_and_missing_required_file_are_rejected(self) -> None:
        report = self.publish()
        path = report / "manifest.json"
        original = path.read_bytes()
        manifest = json.loads(original)
        manifest["schema_version"] = "future_protocol"
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            read_observation_package(report)
        path.write_bytes(original)
        (report / "relations.json").unlink()
        with self.assertRaises(OSError):
            read_observation_package(report)
