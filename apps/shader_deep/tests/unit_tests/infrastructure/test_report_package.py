"""完整五库文件包和按草图视图的交付行为."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep.domain.five_libraries import FiveLibraries, Issue
from shader_deep.infrastructure.storage.report_package import (
    ReferenceAsset,
    ReportManifest,
    read_report_package,
    read_sketch,
    write_report_package,
)


def package_libraries() -> FiveLibraries:
    return FiveLibraries.model_validate(
        {
            "elements": [{"id": "E1", "name": "粉色圆形", "region": "中央圆形本体", "feature_ids": ["F1", "F2"]}],
            "features": [
                {"id": "F1", "description": "左上亮斑", "mechanism_refs": [{"id": "M1"}, {"id": "M2"}]},
                {"id": "F2", "description": "右下亮斑", "mechanism_refs": [{"id": "M1"}]},
            ],
            "relations": [],
            "mechanisms": [
                {"id": "M1", "name": "弧形遮罩", "method": "用曲线距离构造透明度, 参数控制中心和宽度"},
                {"id": "M2", "name": "高斯遮罩", "method": "椭圆高斯控制亮斑透明度, 输出局部颜色"},
            ],
            "sketches": [
                {
                    "id": "S1",
                    "element_id": "E1",
                    "name": "二维分层",
                    "default": {"F1": ["M1"]},
                    "composition": "对 [[F1]] 应用一处局部遮罩",
                    "alternatives": [{"choices": {"F1": ["M2"]}, "reason": "保留亮斑形态的不确定性"}],
                },
                {"id": "S2", "element_id": "E1", "name": "另一个观察方案", "default": {"F2": ["M1"]}, "composition": "对 [[F2]] 单独应用"},
            ],
        }
    )


class ReportPackageTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = b"fixed-reference-bytes"
        self.library = package_libraries()
        self.manifest = ReportManifest(
            reference=ReferenceAsset(sha256=hashlib.sha256(self.reference).hexdigest()), target_element_id="E1", status="completed"
        )

    def publish(self) -> Path:
        return write_report_package(self.root / "report", self.library, self.manifest, self.reference)

    def test_whole_package_can_move_and_default_view_keeps_global_status(self) -> None:
        report = self.publish()
        relocated = self.root / "relocated"
        report.rename(relocated)
        manifest, libraries = read_report_package(relocated)
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(libraries, self.library)
        self.assertEqual(len(list(relocated.iterdir())), 8)
        view = read_sketch(relocated, "S1")
        self.assertEqual(view["scope"], "sketch_view")
        self.assertEqual(view["manifest"]["status"], "completed")
        self.assertEqual([item["id"] for item in view["features"]], ["F1"])
        self.assertEqual([item["id"] for item in view["mechanisms"]], ["M1"])
        self.assertEqual(view["sketch"]["alternatives"][0]["choices"], {"F1": ["M2"]})
        self.assertEqual(len(libraries.sketches), 2)

    def test_explicit_alternative_changes_view_without_changing_package(self) -> None:
        report = self.publish()
        previous = (report / "sketches.json").read_bytes()
        view = read_sketch(report, "S1", alternative=0)
        self.assertEqual(view["selected"]["choices"], {"F1": ["M2"]})
        self.assertEqual([item["id"] for item in view["mechanisms"]], ["M2"])
        self.assertEqual((report / "sketches.json").read_bytes(), previous)
        for invalid in (-1, 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                read_sketch(report, "S1", alternative=invalid)

    def test_global_gaps_remain_visible_for_a_usable_sketch(self) -> None:
        self.manifest = self.manifest.model_copy(update={"status": "partial", "gaps": (Issue(refs=(), description="第三视角失败"),)})
        report = self.publish()
        view = read_sketch(report, "S1")
        self.assertEqual(view["manifest"]["status"], "partial")
        self.assertEqual(view["manifest"]["gaps"][0]["description"], "第三视角失败")
        self.assertIn("第三视角失败", (report / "README.md").read_text())

    def test_failed_staging_never_publishes_half_package(self) -> None:
        with (
            patch("shader_deep.infrastructure.storage.report_package._write_contents", side_effect=OSError("disk failure")),
            self.assertRaises(OSError),
        ):
            self.publish()
        self.assertFalse((self.root / "report").exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hash_and_public_issues_are_checked_before_publication(self) -> None:
        for manifest in (
            self.manifest.model_copy(update={"gaps": (Issue(refs=(), description="整合未完成"),)}),
            self.manifest.model_copy(update={"open_questions": (Issue(refs=("missing",), description="无效引用"),)}),
            self.manifest.model_copy(update={"reference": ReferenceAsset(sha256="0" * 64)}),
        ):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                write_report_package(self.root / "report", self.library, manifest, self.reference)
        self.assertFalse((self.root / "report").exists())

    def test_published_package_cannot_be_replaced_and_corruption_is_detected(self) -> None:
        report = self.publish()
        with self.assertRaises(FileExistsError):
            self.publish()
        (report / "reference.png").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            read_report_package(report)
        payload = json.loads((report / "manifest.json").read_text())
        self.assertEqual(payload["reference"]["sha256"], self.manifest.reference.sha256)
