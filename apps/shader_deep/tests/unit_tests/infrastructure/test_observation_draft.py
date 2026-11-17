"""草稿落盘、局部修订与写入失败的无网络回归."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shader_deep.domain.scene_observations import SceneObservationSubmission, build_scene_observations, diagnose_scene_observations
from shader_deep.infrastructure.storage.json_draft import _atomic_write
from shader_deep.infrastructure.storage.observation_draft import DraftError, DraftStore


def draft_report() -> SceneObservationSubmission:
    return SceneObservationSubmission.model_validate(
        {
            "elements": [
                {"id": "E1", "name": "后圆", "region": "左下", "features": [{"id": "F1", "description": "蓝色圆形"}]},
                {"id": "E2", "name": "前圆", "region": "右上", "features": [{"id": "F1", "description": "灰色圆形"}]},
            ],
            "relations": [
                {"id": "R1", "participants": ["E1", "E2"], "description": "两个圆形部分覆盖"},
                {"id": "R2", "participants": ["E1", "E2-F1"], "description": "底层轮廓变软"},
                {"id": "R3", "participants": ["E1", "E2"], "description": "交叠区域改变颜色"},
            ],
        }
    )


class ObservationDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = DraftStore(self.directory)
        self.report = draft_report()

    def assert_rejected(self, code: str, old: str, new: str, **kwargs: object) -> None:
        before = self.store.read()
        with self.assertRaises(DraftError) as caught:
            self.store.edit(old, new, expected_revision=before[1], **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.read(), before)

    def test_draft_saves_business_errors_and_local_fixes_keep_all_relations(self) -> None:
        receipt = self.store.create(self.report)
        self.assertTrue(receipt["created"])
        self.assertFalse(receipt["validation"]["valid"])
        before, revision = self.store.read()
        self.assertEqual(len(self.store.report().relations), 3)
        old = '"id": "F1",\n          "description": "灰色圆形"'
        edit = self.store.edit(old, old.replace('"F1"', '"F2"'), expected_revision=revision)
        self.assertTrue(edit["saved"])
        self.assertEqual(edit["changes"][0]["path"], "/elements/1/features/0/id")
        self.assertEqual(self.store.report().relations, self.report.relations)
        self.assertEqual((self.store.history / f"{revision}.json").read_text(), before)
        self.store.edit('"E2-F1"', '"F2"', expected_revision=self.store.revision)
        self.assertEqual(len(build_scene_observations(self.store.report()).relations), 3)

    def test_repeated_create_preserves_current_bytes(self) -> None:
        self.store.create(self.report)
        before = self.store.read()
        other = self.report.model_copy(update={"relations": ()})
        self.assertFalse(self.store.create(other)["created"])
        self.assertEqual(self.store.read(), before)

    def test_invalid_json_schema_and_nonlocal_replacements_leave_draft_unchanged(self) -> None:
        self.store.create(self.report)
        self.assert_rejected("invalid_json", '"蓝色圆形"', '"unterminated')
        self.assert_rejected("invalid_json", '"蓝色圆形"', "NaN")
        self.assert_rejected("invalid_json", '"region": "左下"', '"region": "左下", "region": "右下"')
        self.assert_rejected("invalid_fields", '"蓝色圆形"', "42")
        self.assert_rejected("non_unique_replacement", '"F1"', '"F2"')
        self.assert_rejected("non_unique_replacement", '"蓝色圆形"', '"青色圆形"', replace_all=True)
        self.assert_rejected("whole_document_replace", self.store.read()[0], "{}")

    def test_stale_response_cannot_overwrite_a_newer_edit(self) -> None:
        self.store.create(self.report)
        _, revision = self.store.read()
        self.store.edit("蓝色圆形", "青色圆形", expected_revision=revision)
        current = self.store.read()
        with self.assertRaises(DraftError) as caught:
            self.store.edit("灰色圆形", "白色圆形", expected_revision=revision)
        self.assertEqual(caught.exception.code, "stale_draft")
        self.assertEqual(self.store.read(), current)

    def test_external_change_or_symlink_is_detected(self) -> None:
        self.store.create(self.report)
        self.store.path.write_text("{}")
        with self.assertRaises(DraftError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "external_draft_change")
        self.store.path.unlink()
        target = self.directory / "protected.json"
        target.write_text("protected")
        self.store.path.symlink_to(target)
        with self.assertRaises(DraftError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "invalid_path")
        self.assertEqual(target.read_text(), "protected")

    def test_snapshot_and_commit_failure_preserve_old_draft(self) -> None:
        self.store.create(self.report)
        before = self.store.read()
        with (
            patch("shader_deep.infrastructure.storage.json_draft._atomic_write", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.store.edit("蓝色圆形", "青色圆形", expected_revision=before[1])
        self.assertEqual(self.store.read(), before)

        def fail_current(path: Path, content: bytes) -> None:
            if path == self.store.path:
                msg = "cannot replace draft"
                raise OSError(msg)
            _atomic_write(path, content)

        with patch("shader_deep.infrastructure.storage.json_draft._atomic_write", side_effect=fail_current), self.assertRaises(OSError):
            self.store.edit("蓝色圆形", "青色圆形", expected_revision=before[1])
        self.assertEqual(self.store.read(), before)
        self.assertEqual((self.store.history / f"{before[1]}.json").read_text(), before[0])

    def test_post_commit_diagnostic_failure_keeps_committed_revision(self) -> None:
        self.store.create(self.report)
        previous = self.store.revision
        with (
            patch.object(self.store, "validation", side_effect=RuntimeError("diagnostics failed")),
            self.assertRaises(RuntimeError),
        ):
            self.store.edit("蓝色圆形", "青色圆形", expected_revision=previous)
        self.assertNotEqual(self.store.revision, previous)
        self.assertIn("青色圆形", self.store.read()[0])

    def test_atomic_writer_cleans_temporary_file_when_replace_fails(self) -> None:
        path = self.directory / "file.json"
        path.write_text("old")
        with patch.object(Path, "replace", side_effect=OSError("replace failed")), self.assertRaises(OSError):
            _atomic_write(path, b"new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.directory.iterdir()), [path])

    def test_cleanup_failure_cannot_mask_committed_bytes_and_revision(self) -> None:
        self.store.create(self.report)
        before = self.store.revision
        with patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
            self.store.edit("蓝色圆形", "青色圆形", expected_revision=before)
        self.assertNotEqual(self.store.revision, before)
        self.assertIn("青色圆形", self.store.read()[0])

    def test_noop_does_not_create_history_and_foreign_draft_cannot_be_adopted(self) -> None:
        self.store.create(self.report)
        before = self.store.read()
        result = self.store.edit("蓝色圆形", "蓝色圆形", expected_revision=before[1])
        self.assertEqual(result["changes"], [])
        self.assertEqual(self.store.read(), before)
        self.assertFalse(self.store.history.exists())
        other = DraftStore(self.directory)
        with self.assertRaises(DraftError) as caught:
            other.create(self.report)
        self.assertEqual(caught.exception.code, "external_draft_change")

    def test_diagnostics_keep_all_conflict_locations_and_reference_paths(self) -> None:
        report = self.report.model_copy(update={"gaps": ()})
        diagnostics = diagnose_scene_observations(report)
        duplicate = next(issue for issue in diagnostics if issue.code == "duplicate_id")
        self.assertEqual(duplicate.related_paths, ("/elements/0/features/0/id", "/elements/1/features/0/id"))
        missing = next(issue for issue in diagnostics if issue.code == "missing_reference")
        self.assertEqual(missing.path, "/relations/1/participants/1")
        current = self.report.model_dump(mode="json")
        current["relations"][1]["participants"] = ["F1", "R1", "R1"]
        codes = {issue.code for issue in diagnose_scene_observations(SceneObservationSubmission.model_validate(current))}
        self.assertTrue({"ambiguous_reference", "reference_type", "duplicate_reference"}.issubset(codes))
