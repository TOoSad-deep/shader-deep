"""验证材料身份、完整回读、裁剪及目录隔离, 不调用模型或浏览器."""

from __future__ import annotations

import io
import json
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from shader_deep.infrastructure.storage.visual_memory import (
    MaterialError,
    MaterialRegion,
    MaterialSource,
    MemoryLimits,
    VisualMemory,
)


class VisualMemoryTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = self.root / "run-one"
        self.run.mkdir()
        self.store = VisualMemory(self.run, "G1")
        self.source = MaterialSource(type="output", task_id="G1", operation="render_shader", candidate_id="c001")

    def code(self, name: str = "candidate.glsl", content: str = "void mainImage() {}") -> str:
        path = self.run / name
        path.write_text(content, encoding="utf-8")
        return self.store.register(path, kind="code", label=name, source=self.source).id

    def image(self) -> str:
        path = self.run / "candidate.png"
        with Image.new("RGBA", (8, 6)) as picture:
            picture.putdata([(x * 17, y * 23, 99, (x + y) * 13) for y in range(6) for x in range(8)])
            picture.save(path)
        return self.store.register(path, kind="image", label="候选整图", source=self.source).id

    def test_index_stable_identity_and_distinct_candidate_provenance(self) -> None:
        first = self.code()
        second = self.store.register(
            "candidate.glsl",
            kind="code",
            label="另一候选代码",
            source=MaterialSource(type="output", task_id="G1", operation="render_shader", candidate_id="c002"),
        )
        reopened = VisualMemory(self.run, "G1")
        self.assertNotEqual(first, second.id)
        self.assertEqual(reopened.get(first).sha256, second.sha256)
        self.assertEqual(reopened.get(first).source.candidate_id, "c001")
        self.assertEqual(reopened.get(second.id).source.candidate_id, "c002")
        self.assertEqual(reopened.read(first).text, "void mainImage() {}")
        self.assertEqual(len(reopened), 2)
        index = json.loads((self.run / "memory/index.json").read_text())
        self.assertNotIn("id", index["materials"][first])
        self.assertEqual(index["materials"][first]["path"], "candidate.glsl")

    def test_crop_preserves_rgba_and_reuses_identity_with_direct_parent(self) -> None:
        original = self.image()
        region = MaterialRegion(x=2, y=1, width=4, height=3)
        result = self.store.read(original, region=region)
        repeated = self.store.read(original, region=region)
        self.assertEqual(result.record.id, repeated.record.id)
        self.assertEqual(result.image_size, (4, 3))
        self.assertEqual(len(self.store), 2)
        with Image.open(io.BytesIO(result.data)) as crop, Image.open(self.run / "candidate.png") as image, image.crop((2, 1, 6, 4)) as expected:
            self.assertEqual(crop.mode, "RGBA")
            self.assertEqual(crop.tobytes(), expected.tobytes())
        nested = self.store.read(result.record.id, region=MaterialRegion(x=1, y=0, width=2, height=2))
        self.assertEqual(nested.record.derived_from.material_id, result.record.id)
        self.assertEqual(nested.record.derived_from.region.x, 1)

    def test_invalid_region_and_non_image_crop_do_not_create_material(self) -> None:
        code, image = self.code(), self.image()
        for identifier, region in ((code, MaterialRegion(x=0, y=0, width=1, height=1)), (image, MaterialRegion(x=7, y=5, width=2, height=1))):
            with self.subTest(identifier=identifier), self.assertRaises(MaterialError) as caught:
                self.store.read(identifier, region=region)
            self.assertEqual(caught.exception.code, "invalid_region")
        self.assertEqual(len(self.store), 2)
        with self.assertRaises(ValidationError):
            MaterialRegion(x=True, y=0, width=1, height=1)

    def test_pagination_survives_append_and_binds_scope_and_filter(self) -> None:
        first, second = self.code("one.glsl"), self.code("two.glsl")
        self.image()
        page = self.store.list_materials(kind="code", limit=1)
        third = self.code("three.glsl")
        next_page = self.store.list_materials(kind="code", cursor=page["next_cursor"])
        self.assertEqual([item["id"] for item in page["materials"]], [first])
        self.assertEqual([item["id"] for item in next_page["materials"]], [second, third])
        self.assertIsNone(next_page["next_cursor"])
        other = self.root / "other" / "run-one"
        other.mkdir(parents=True)
        for store, kind in ((self.store, "image"), (VisualMemory(other, "G1"), "code"), (VisualMemory(self.run, "G2"), "code")):
            with self.subTest(root=store.run_dir, kind=kind), self.assertRaises(MaterialError) as caught:
                store.list_materials(kind=kind, cursor=page["next_cursor"])
            self.assertEqual(caught.exception.code, "invalid_cursor")

    def test_directory_returns_only_compact_descriptions(self) -> None:
        identifier = self.code()
        row = self.store.list_materials()["materials"][0]
        self.assertEqual(row, {"id": identifier, "kind": "code", "label": "candidate.glsl", "candidate_id": "c001"})
        for limit in (0, 51, True):
            with self.subTest(limit=limit), self.assertRaises(MaterialError):
                self.store.list_materials(limit=limit)

    def test_other_task_and_unknown_id_share_unavailable_error(self) -> None:
        identifier = self.code()
        other = VisualMemory(self.run, "G2")
        self.assertEqual(len(other), 0)
        self.assertEqual(other.list_materials()["materials"], [])
        for selected in (identifier, "m9999"):
            with self.subTest(selected=selected), self.assertRaises(MaterialError) as caught:
                other.read(selected)
            self.assertEqual(caught.exception.code, "unknown_material")

    def test_changed_missing_or_escaped_file_is_not_replaced(self) -> None:
        identifier = self.code()
        path = self.run / "candidate.glsl"
        path.write_text("changed")
        with self.assertRaises(MaterialError) as caught:
            self.store.read(identifier)
        self.assertEqual(caught.exception.code, "content_changed")
        path.unlink()
        with self.assertRaises(MaterialError) as caught:
            self.store.read(identifier)
        self.assertEqual(caught.exception.code, "missing_file")
        external = self.root / "outside.glsl"
        external.write_text("void mainImage() {}")
        path.symlink_to(external)
        with self.assertRaises(MaterialError) as caught:
            self.store.read(identifier)
        self.assertEqual(caught.exception.code, "invalid_path")
        with self.assertRaises(MaterialError):
            self.store.register(external, kind="code", label="越界", source=self.source)

    def test_failed_index_publish_does_not_expose_new_record(self) -> None:
        self.code()
        before = (self.run / "memory/index.json").read_bytes()
        with patch("shader_deep.infrastructure.storage.visual_memory.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.code("second.glsl")
        self.assertEqual(len(self.store), 1)
        self.assertEqual((self.run / "memory/index.json").read_bytes(), before)
        with self.assertRaises(MaterialError):
            self.store.get("m0002")
        self.assertEqual(self.store.register("second.glsl", kind="code", label="second", source=self.source).id, "m0002")

    def test_index_rejects_duplicate_keys_version_and_broken_derivation(self) -> None:
        original = self.image()
        self.store.read(original, region=MaterialRegion(x=0, y=0, width=2, height=2))
        path = self.run / "memory/index.json"
        payload = json.loads(path.read_text())
        duplicate = path.read_text().replace('"version": 1', '"version": 1, "version": 1')
        for content in (duplicate, json.dumps({**payload, "version": 2})):
            path.write_text(content)
            with self.assertRaises(MaterialError) as caught:
                VisualMemory(self.run, "G1")
            self.assertEqual(caught.exception.code, "invalid_index")
        payload["materials"]["m0002"]["derived_from"]["material_id"] = "m9999"
        path.write_text(json.dumps(payload))
        with self.assertRaises(MaterialError):
            VisualMemory(self.run, "G1")

    def test_limits_reject_expansion_without_deleting_material(self) -> None:
        code, image = self.code(content="abcde"), self.image()
        small = VisualMemory(self.run, "G1", limits=MemoryLimits(max_text_chars=4, max_image_pixels=47))
        for identifier in (code, image):
            with self.subTest(identifier=identifier), self.assertRaises(MaterialError) as caught:
                small.read(identifier)
            self.assertEqual(caught.exception.code, "content_too_large")
        self.assertEqual(self.store.read(code).text, "abcde")
        self.assertEqual(self.store.read(image).image_size, (8, 6))

    def test_json_material_reads_actual_text_and_rejects_invalid_content(self) -> None:
        path = self.run / "result.json"
        path.write_text('{"error": "compile failed"}')
        record = self.store.register(path, kind="json", label="渲染错误", source=self.source)
        self.assertEqual(json.loads(self.store.read(record.id).text), {"error": "compile failed"})
        for index, value in enumerate(('{"error":', '{"value": NaN}', '{"value": 1, "value": 2}')):
            bad = self.run / f"invalid-{index}.json"
            bad.write_text(value)
            record = self.store.register(bad, kind="json", label="无效资料", source=self.source)
            with self.subTest(value=value), self.assertRaises(MaterialError) as caught:
                self.store.read(record.id)
            self.assertEqual(caught.exception.code, "invalid_content")

    def test_symlinked_index_temporary_cannot_write_outside_run(self) -> None:
        self.code()
        external = self.root / "keep.json"
        external.write_text("keep")
        (self.run / "memory/index.tmp").symlink_to(external)
        with self.assertRaises(MaterialError):
            self.code("second.glsl")
        self.assertEqual(external.read_text(), "keep")
        self.assertEqual(len(self.store), 1)

    def test_crop_publish_failure_cleans_only_new_file_and_retry_succeeds(self) -> None:
        identifier = self.image()
        region = MaterialRegion(x=1, y=1, width=2, height=2)
        before = (self.run / "memory/index.json").read_bytes()
        with patch("shader_deep.infrastructure.storage.visual_memory.write_snapshot", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.read(identifier, region=region)
        self.assertEqual(len(self.store), 1)
        self.assertEqual((self.run / "memory/index.json").read_bytes(), before)
        self.assertFalse((self.run / "memory/derived/m0002.png").exists())
        self.assertEqual(self.store.read(identifier, region=region).record.id, "m0002")

    def test_existing_crop_path_is_not_deleted_when_creation_fails(self) -> None:
        identifier = self.image()
        directory = self.run / "memory/derived"
        directory.mkdir()
        existing = directory / "m0002.png"
        existing.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.store.read(identifier, region=MaterialRegion(x=1, y=1, width=2, height=2))
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_png_bomb_warning_and_error_become_size_errors(self) -> None:
        original = self.image()
        content = self.store.read(original).data
        for side in (10_000, 100_000):
            data = bytearray(content)
            data[16:24] = struct.pack(">II", side, side)
            data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]) & 0xFFFFFFFF)
            path = self.run / f"large-{side}.png"
            path.write_bytes(data)
            record = self.store.register(path, kind="image", label="尺寸异常图像", source=self.source)
            with self.subTest(side=side), self.assertRaises(MaterialError) as caught:
                self.store.read(record.id)
            self.assertEqual(caught.exception.code, "content_too_large")
        self.assertEqual(self.store.read(original).image_size, (8, 6))
