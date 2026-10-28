"""证据的固定字节、像素语义与包内路径校验, 不依赖旧执行协议."""

from __future__ import annotations

import base64
import hashlib
import io
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest import TestCase

from PIL import Image

from shader_deep.agents.analysis.observations import ReferenceObservations
from shader_deep.domain.evidence import ImageRegion, MeasurementRequest, MeasurementSpec
from shader_deep.domain.five_libraries import ExplorationSubmission
from shader_deep.domain.five_libraries.validation import exploration_library
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.storage.analysis_materials import package_materials
from shader_deep.infrastructure.storage.report_evidence import EvidenceAsset, ReportEvidence, evidence_bytes
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, read_report_package, write_report_package
from tests.unit_tests.domain.test_five_libraries import report, target


class ReportEvidenceTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with Image.new("RGBA", (20, 16), (10, 20, 30, 128)) as image:
            stream = io.BytesIO()
            image.save(stream, format="PNG")
        self.reference = stream.getvalue()
        self.digest = hashlib.sha256(self.reference).hexdigest()
        self.url = "data:image/png;base64," + base64.b64encode(self.reference).decode()
        self.worker = ReferenceObservations(self.url, self.root / "observations", "explorer-1", 3)
        self.target = target()

    def _accept(self, value: ExplorationSubmission) -> None:
        self.materials = self.worker.snapshot(value)

    def test_presented_bytes_survive_source_change_and_cleanup(self) -> None:
        value = self._record()
        if self.record.artifact_path is None:
            self.fail("缺少裁剪")
        original = Path(self.record.artifact_path).read_bytes()
        Path(self.record.artifact_path).write_bytes(b"changed after presentation")
        self._accept(value)
        shutil.rmtree(self.worker.measurements.directory)
        packed, assets = package_materials(self.materials, self.digest)
        if packed is None:
            self.fail("缺少证据")
        self.assertEqual(tuple(assets.values()), (original,))
        read_report_package(self._write(packed, assets, value, "frozen"))

    def _record(self, kind: Literal["crop", "region_stats", "line_profile"] = "crop") -> ExplorationSubmission:
        record, _ = self.worker.measurements.measure(
            MeasurementRequest(question="观察", measurement=MeasurementSpec(kind=kind, region=ImageRegion(left=2, top=3, right=10, bottom=12)))
        )
        self.record = record
        self.worker.mark_presented([self.worker.context(_message({}, self.url))])
        return ExplorationSubmission.model_validate({**report().model_dump(mode="json"), "evidence_links": {"F1": [record.id]}})

    def _write(self, evidence: ReportEvidence, assets: dict[str, bytes], value: ExplorationSubmission, name: str) -> Path:
        declaration = EvidenceAsset(path="evidence.json", sha256=hashlib.sha256(evidence_bytes(evidence)).hexdigest())
        manifest = ReportManifest(reference=ReferenceAsset(sha256=self.digest), target_element_id="E1", status="completed", evidence=declaration)
        return write_report_package(
            self.root / name, exploration_library(value, self.target), manifest, self.reference, evidence=evidence, evidence_assets=assets
        )

    def test_crop_requires_png_and_exact_reference_region(self) -> None:
        value = self._record()
        self._accept(value)
        packed, _assets = package_materials(self.materials, self.digest)
        if packed is None:
            self.fail("缺少证据")
        missing = packed.model_copy(update={"records": (replace(packed.records[0], artifact_path=None),), "assets": ()})
        with self.assertRaisesRegex(ValueError, "缺少实际图像"):
            self._write(missing, {}, value, "missing")
        for name, size in (("wrong-size", (1, 1)), ("wrong-pixels", (8, 9))):
            with Image.new("RGBA", size, "red") as image:
                stream = io.BytesIO()
                image.save(stream, format="PNG")
            data = stream.getvalue()
            asset = packed.assets[0].model_copy(update={"sha256": hashlib.sha256(data).hexdigest()})
            invalid = packed.model_copy(update={"assets": (asset,)})
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "原图区域"):
                self._write(invalid, {asset.path: data}, value, name)
        data = b"not a PNG"
        asset = packed.assets[0].model_copy(update={"sha256": hashlib.sha256(data).hexdigest()})
        with self.assertRaisesRegex(ValueError, "无法解码"):
            self._write(packed.model_copy(update={"assets": (asset,)}), {asset.path: data}, value, "not-png")

    def test_statistics_require_finite_results_and_correct_semantics(self) -> None:
        value = self._record("region_stats")
        self._accept(value)
        packed, assets = package_materials(self.materials, self.digest)
        if packed is None:
            self.fail("缺少证据")
        read_report_package(self._write(packed, assets, value, "valid-stats"))
        for name, changes in (
            ("missing", {"mean_rgb": None}),
            ("nan", {"mean_luma": float("nan")}),
            ("wrong-alpha", {"alpha_policy": "raw"}),
            ("wrong-count", {"samples_per_value": 1}),
        ):
            invalid = packed.model_copy(update={"records": (replace(packed.records[0], **changes),)})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self._write(invalid, {}, value, name)

    def test_measurement_kinds_reject_foreign_fields_and_invalid_profile_digest(self) -> None:
        value = self._record()
        crop = self.record
        self._record("region_stats")
        stats = replace(self.record, id=crop.id)
        self._record("line_profile")
        profile = replace(self.record, id=crop.id)
        digest = profile.profile_digest
        if digest is None:
            self.fail("缺少剖面摘要")
        self._accept(value)
        packed, assets = package_materials(self.materials, self.digest)
        if packed is None:
            self.fail("缺少证据")
        crop = packed.records[0]
        for name, record in (
            ("stats-profile", replace(stats, profile=(float("nan"),))),
            ("stats-digest", replace(stats, profile_digest=digest)),
            ("profile-rgb", replace(profile, mean_rgb=(1.0, 2.0, 3.0))),
            ("profile-luma", replace(profile, mean_luma=float("inf"))),
            ("profile-digest-nan", replace(profile, profile_digest=replace(digest, minimum_contrast=float("nan")))),
            ("profile-digest-point", replace(profile, profile_digest=replace(digest, samples=(replace(digest.samples[0], value=float("inf")),)))),
            ("crop-digest", replace(crop, profile_digest=digest)),
        ):
            is_crop = record.spec.kind == "crop"
            invalid = packed.model_copy(update={"records": (record,), "assets": packed.assets if is_crop else ()})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self._write(invalid, assets if is_crop else {}, value, name)
        valid = packed.model_copy(update={"records": (profile,), "assets": ()})
        read_report_package(self._write(valid, {}, value, "valid-profile"))
