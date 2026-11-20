"""视觉评审的双图捕获、局部视图和正式包验证."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from shader_deep.domain.visual_review import PixelRegion, ReviewRequest, ReviewSource
from shader_deep.imaging.png16 import decode_png16
from shader_deep.infrastructure.storage.planning_inputs import content_sha256
from shader_deep.infrastructure.storage.review_package import (
    CapturedReviewSource,
    capture_review_source,
    read_review_package,
    write_review_package,
)
from tests.unit_tests.fixtures._review_fixture import matched_review, review_request


def png(color: str) -> bytes:
    output = io.BytesIO()
    with Image.new("RGBA", (16, 12), color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def filtered_row(row: bytes, previous: bytes, stride: int, method: int) -> bytes:
    output = bytearray()
    for offset, value in enumerate(row):
        left = row[offset - stride] if offset >= stride else 0
        above = previous[offset]
        corner = previous[offset - stride] if offset >= stride else 0
        estimate = left + above - corner
        nearest = min((left, above, corner), key=lambda item: abs(item - estimate))
        predictor = (0, left, above, (left + above) // 2, nearest)[method]
        output.append((value - predictor) % 256)
    return bytes([method]) + output


def png16(
    width: int,
    height: int,
    color_type: int,
    samples: tuple[int, ...],
    *,
    interlaced: bool = False,
    method: int | None = None,
    transparency: bytes | None = None,
    compressed: bytes | None = None,
) -> bytes:
    """从显式 16 位样本构造独立测试输入, 用标准 8x8 pass 标号表编码 Adam7."""
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = struct.pack(f">{len(samples)}H", *samples)
    grid = (
        (1, 6, 4, 6, 2, 6, 4, 6),
        (7,) * 8,
        (5, 6, 5, 6, 5, 6, 5, 6),
        (7,) * 8,
        (3, 6, 4, 6, 3, 6, 4, 6),
        (7,) * 8,
        (5, 6, 5, 6, 5, 6, 5, 6),
        (7,) * 8,
    )
    scans = bytearray()
    for pass_id in range(1, 8) if interlaced else (0,):
        previous: bytes | None = None
        for y in range(height):
            columns = [x for x in range(width) if not interlaced or grid[y % 8][x % 8] == pass_id]
            if not columns:
                continue
            stride = channels * 2
            row = b"".join(raw[(y * width + x) * stride : (y * width + x + 1) * stride] for x in columns)
            scans.extend(filtered_row(row, previous or bytes(len(row)), stride, method if method is not None else y % 5))
            previous = row
    header = struct.pack(">IIBBBBB", width, height, 16, color_type, 0, 0, int(interlaced))
    alpha = png_chunk(b"tRNS", transparency) if transparency is not None else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + alpha
        + png_chunk(b"IDAT", compressed if compressed is not None else zlib.compress(scans))
        + png_chunk(b"IEND", b"")
    )


class ReviewPackageTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference, self.rendered = self.root / "reference.png", self.root / "rendered.png"
        self.reference.write_bytes(png("red"))
        self.rendered.write_bytes(png("blue"))

    def capture(self, name: str = "run", request: ReviewRequest | None = None) -> CapturedReviewSource:
        return capture_review_source(self.reference, self.rendered, self.root / name, request or review_request())

    def publish(self) -> Path:
        source = self.capture()
        return write_review_package(self.root / "report", source, matched_review(source.criteria), metadata={"model_calls": 1})

    def reseal(self, directory: Path) -> None:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        contents = {name: (directory / name).read_bytes() for name in manifest["files"]}
        manifest["files"] = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
        manifest["content_sha256"] = content_sha256(contents)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_roundtrip_relocation_preserves_fixed_bytes_and_report(self) -> None:
        original = self.publish()
        package = read_review_package(original)
        self.assertEqual(package.report.verdict, "no_major_issue_observed")
        self.assertEqual(package.report.metadata, {"model_calls": 1})
        moved = self.root / "moved"
        original.rename(moved)
        loaded = read_review_package(moved)
        self.assertEqual(loaded.content_sha256, package.content_sha256)
        self.assertEqual(loaded.source.directory, moved)
        self.assertEqual(base64.b64decode(loaded.source.reference_url.split(",", 1)[1]), self.reference.read_bytes())

    def test_original_and_captured_files_changes_do_not_replace_model_material(self) -> None:
        source = self.capture()
        expected_images = source.images
        self.reference.write_bytes(png("green"))
        self.rendered.unlink()
        (source.directory / "inputs/reference.png").write_bytes(png("white"))
        report = write_review_package(self.root / "report", source, matched_review(source.criteria))
        package = read_review_package(report)
        self.assertEqual(package.source.images, expected_images)
        self.assertNotEqual((report / "reference.png").read_bytes(), self.reference.read_bytes())

    def test_local_crops_keep_exact_pixels_coordinates_and_full_context(self) -> None:
        region = PixelRegion(left=2, top=3, right=9, bottom=8)
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "feature", "reference_region": region, "rendered_region": region}
        )
        source = self.capture(request=request)
        self.assertEqual(len(source.images), 4)
        self.assertIn('"left":2', source.images[2][0])
        crop_bytes = base64.b64decode(source.images[2][1].split(",", 1)[1])
        with Image.open(io.BytesIO(crop_bytes)) as cropped:
            self.assertEqual(cropped.size, (7, 5))
            self.assertEqual(cropped.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 255))
        report = write_review_package(self.root / "report", source, matched_review(source.criteria))
        self.assertEqual(read_review_package(report).source.request.reference_region, region)

    def test_source_declaration_changes_input_identity(self) -> None:
        first = self.capture()
        request = review_request().model_copy(update={"source": ReviewSource(candidate_id="candidate-b", run_id="older-run")})
        second = self.capture("another", request)
        self.assertEqual(first.criteria.criteria_sha256, second.criteria.criteria_sha256)
        self.assertNotEqual(first.input_sha256, second.input_sha256)

    def test_local_views_preserve_png_color_metadata(self) -> None:
        info = PngInfo()
        info.add(b"gAMA", struct.pack(">I", 100000))
        info.add(b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000))
        info.add(b"sRGB", b"\0")
        for name, profile in (("gamma-srgb", None), ("icc", b"fixed ICC profile bytes")):
            output = io.BytesIO()
            with Image.new("RGBA", (16, 12), (128, 128, 128, 180)) as image:
                image.save(output, format="PNG", pnginfo=info, icc_profile=profile)
            self.reference.write_bytes(output.getvalue())
            region = PixelRegion(left=1, top=2, right=8, bottom=6)
            request = review_request().model_copy(update={"scope": "local", "target_kind": "feature", "reference_region": region})
            source = self.capture(name, request)
            report = write_review_package(self.root / f"{name}-report", source, matched_review(source.criteria))
            package = read_review_package(report)
            with Image.open(io.BytesIO(output.getvalue())) as original, Image.open(report / "views/reference.png") as cropped:
                for key in ("gamma", "chromaticity", "srgb", "icc_profile"):
                    with self.subTest(profile=name, metadata=key):
                        self.assertEqual(cropped.info.get(key), original.info.get(key))
                self.assertEqual(cropped.convert("RGBA").getpixel((0, 0)), (128, 128, 128, 180))
            self.assertEqual(len(package.source.images), 3)

    def test_removed_color_metadata_is_rejected_after_manifest_resealed(self) -> None:
        output = io.BytesIO()
        info = PngInfo()
        info.add(b"gAMA", struct.pack(">I", 100000))
        with Image.new("RGB", (16, 12), "gray") as image:
            image.save(output, format="PNG", pnginfo=info)
        self.reference.write_bytes(output.getvalue())
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "feature", "reference_region": PixelRegion(left=0, top=0, right=16, bottom=12)}
        )
        source = self.capture(request=request)
        directory = write_review_package(self.root / "report", source, matched_review(source.criteria))
        crop_path = directory / "views/reference.png"
        with Image.open(crop_path) as image:
            without_metadata = io.BytesIO()
            image.save(without_metadata, format="PNG")
        crop_path.write_bytes(without_metadata.getvalue())
        self.reseal(directory)
        with self.assertRaisesRegex(ValueError, "颜色元数据"):
            read_review_package(directory)

    def test_16bit_views_preserve_samples_and_transparency_across_color_types_and_adam7(self) -> None:
        width, height = 11, 9
        region = PixelRegion(left=2, top=1, right=9, bottom=7)
        for color_type, channels in ((0, 1), (2, 3), (4, 2), (6, 4)):
            for interlaced in (False, True):
                samples = tuple((10000 + index * 997) % 65536 for index in range(width * height * channels))
                transparency = struct.pack(f">{channels}H", *samples[:channels]) if color_type in (0, 2) else None
                self.reference.write_bytes(png16(width, height, color_type, samples, interlaced=interlaced, transparency=transparency))
                request = review_request().model_copy(update={"scope": "local", "target_kind": "element", "reference_region": region})
                source = self.capture(f"color-{color_type}-{interlaced}", request)
                directory = write_review_package(self.root / f"report-{color_type}-{interlaced}", source, matched_review(source.criteria))
                package = read_review_package(directory)
                crop = dict(package.source.contents)["views/reference.png"]
                actual = decode_png16(crop)
                expected = tuple(
                    samples[(y * width + x) * channels + channel]
                    for y in range(region.top, region.bottom)
                    for x in range(region.left, region.right)
                    for channel in range(channels)
                )
                with self.subTest(color_type=color_type, interlaced=interlaced):
                    self.assertEqual(crop[24:26], bytes((16, color_type)))
                    self.assertEqual(actual.samples, struct.pack(f">{len(expected)}H", *expected))
                    self.assertEqual(actual.transparency, transparency)

    def test_rgb16_transparent_key_pixel_survives_capture_and_publication(self) -> None:
        key = struct.pack(">3H", 10000, 20000, 30000)
        self.reference.write_bytes(png16(2, 1, 2, (50000, 40000, 30000, 10000, 20000, 30000), transparency=key))
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "feature", "reference_region": PixelRegion(left=1, top=0, right=2, bottom=1)}
        )
        source = self.capture(request=request)
        directory = write_review_package(self.root / "report", source, matched_review(source.criteria))
        crop = dict(read_review_package(directory).source.contents)["views/reference.png"]
        decoded = decode_png16(crop)
        self.assertEqual(decoded.samples, key)
        self.assertEqual(decoded.transparency, key)
        self.assertEqual(crop[24], 16)

    def test_low_depth_gray_transparency_survives_crop_and_publication(self) -> None:
        for depth in (1, 2, 4):
            # 三个样本为 0, 1, 0. 裁掉首个像素, 透明色键为非零灰阶 1.
            header = struct.pack(">IIBBBBB", 3, 1, depth, 0, 0, 0, 0)
            row = (1 << depth).to_bytes((3 * depth + 7) // 8, "big")
            padding = -3 * depth % 8
            row = (int.from_bytes(row, "big") << padding).to_bytes(len(row), "big")
            self.reference.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + png_chunk(b"IHDR", header)
                + png_chunk(b"tRNS", b"\0\1")
                + png_chunk(b"IDAT", zlib.compress(b"\0" + row))
                + png_chunk(b"IEND", b"")
            )
            request = review_request().model_copy(
                update={
                    "scope": "local",
                    "target_kind": "feature",
                    "reference_region": PixelRegion(left=1, top=0, right=3, bottom=1),
                }
            )
            source = self.capture(f"gray-{depth}", request)
            directory = write_review_package(self.root / f"report-{depth}", source, matched_review(source.criteria))
            package = read_review_package(directory)
            view = dict(package.source.contents)["views/reference.png"]
            level = 255 // ((1 << depth) - 1)
            with self.subTest(depth=depth), Image.open(io.BytesIO(view)) as image:
                self.assertEqual(list(image.convert("RGBA").get_flattened_data()), [(level, level, level, 0), (0, 0, 0, 255)])
            # 同样的灰度像素但透明键已删除, 即使重封摘要, 正式读取仍应拒绝.
            with Image.new("L", (2, 1)) as opaque:
                opaque.putpixel((0, 0), level)
                opaque.save(directory / "views/reference.png")
            self.reseal(directory)
            with self.assertRaisesRegex(ValueError, "局部视图"):
                read_review_package(directory)

    def test_scene_checks_png_chunk_integrity_on_capture_and_read(self) -> None:
        valid = self.reference.read_bytes()
        idat = valid.index(b"IDAT")
        crc = idat + 4 + int.from_bytes(valid[idat - 4 : idat], "big")
        corrupt = valid[:crc] + bytes([valid[crc] ^ 1]) + valid[crc + 1 :]
        directory = self.publish()
        for invalid in (valid[:-12], valid[:-1], corrupt, valid + b"junk", valid[:-12] + png_chunk(b"IEND", b"x")):
            with self.subTest(invalid=invalid[-16:]):
                self.reference.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    self.capture("bad")
                (directory / "reference.png").write_bytes(invalid)
                self.reseal(directory)
                with self.assertRaisesRegex(ValueError, "PNG"):
                    read_review_package(directory)

    def test_changed_gray16_sample_is_rejected_even_when_rgba8_would_match(self) -> None:
        self.reference.write_bytes(png16(1, 1, 0, (10000,)))
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "feature", "reference_region": PixelRegion(left=0, top=0, right=1, bottom=1)}
        )
        source = self.capture(request=request)
        directory = write_review_package(self.root / "report", source, matched_review(source.criteria))
        (directory / "views/reference.png").write_bytes(png16(1, 1, 0, (50000,)))
        self.reseal(directory)
        with self.assertRaisesRegex(ValueError, "16 位样本"):
            read_review_package(directory)

    def test_changed_rgb16_transparency_is_rejected_after_resealing(self) -> None:
        samples = (10000, 20000, 30000)
        self.reference.write_bytes(png16(1, 1, 2, samples, transparency=struct.pack(">3H", *samples)))
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "feature", "reference_region": PixelRegion(left=0, top=0, right=1, bottom=1)}
        )
        source = self.capture(request=request)
        directory = write_review_package(self.root / "report", source, matched_review(source.criteria))
        (directory / "views/reference.png").write_bytes(png16(1, 1, 2, samples, transparency=struct.pack(">3H", 40000, 20000, 30000)))
        self.reseal(directory)
        with self.assertRaisesRegex(ValueError, "透明语义"):
            read_review_package(directory)

    def test_png16_decoder_accepts_all_filters_and_empty_adam7_passes(self) -> None:
        samples = (10000, 50000, 32768, 65535, 0, 12500)
        for method in range(5):
            content = png16(3, 2, 0, samples, method=method)
            with self.subTest(method=method):
                self.assertEqual(decode_png16(content).samples, struct.pack(">6H", *samples))
        self.assertEqual(
            decode_png16(png16(1, 1, 6, (10000, 20000, 30000, 40000), interlaced=True)).samples, struct.pack(">4H", 10000, 20000, 30000, 40000)
        )

    def test_png16_decoder_enforces_exact_bounded_stream_and_filters(self) -> None:
        valid = zlib.compress(b"\0" + struct.pack(">H", 10000))
        broken = (valid[:-1], valid + b"extra", valid + valid, zlib.compress(b"\0" * 100000), zlib.compress(b"\5\0\0"))
        for compressed in broken:
            with self.subTest(compressed=compressed[:8]), self.assertRaises(ValueError):
                decode_png16(png16(1, 1, 0, (10000,), compressed=compressed))

    def test_invalid_png_animation_capacity_and_region_are_rejected(self) -> None:
        self.reference.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            self.capture()
        self.assertFalse((self.root / "run/inputs").exists())
        self.reference.write_bytes(png("red"))
        request = review_request().model_copy(
            update={"scope": "local", "target_kind": "element", "reference_region": PixelRegion(left=0, top=0, right=17, bottom=1)}
        )
        with self.assertRaises(ValueError):
            self.capture(request=request)
        with patch("shader_deep.infrastructure.storage.review_package.MAX_IMAGE_PIXELS", 10), self.assertRaises(ValueError):
            self.capture()
        output = io.BytesIO()
        with Image.new("RGBA", (16, 12), "red") as first, Image.new("RGBA", (16, 12), "blue") as second:
            first.save(output, format="PNG", save_all=True, append_images=[second], duration=100)
        self.reference.write_bytes(output.getvalue())
        with self.assertRaises(ValueError):
            self.capture()

    def test_tampered_png_or_criteria_is_rejected(self) -> None:
        for name in ("reference.png", "criteria.json", "rules/profile.md"):
            source = self.capture("run-" + name.replace("/", "-"))
            directory = write_review_package(self.root / (name.replace("/", "-") + "-report"), source, matched_review(source.criteria))
            (directory / name).write_bytes(b"changed")
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_review_package(directory)

    def test_derived_conclusion_is_checked_even_after_manifest_resealed(self) -> None:
        directory = self.publish()
        path = directory / "review.json"
        report = json.loads(path.read_bytes())
        report["verdict"] = "needs_revision"
        path.write_text(json.dumps(report), encoding="utf-8")
        self.reseal(directory)
        with self.assertRaisesRegex(ValueError, "结论"):
            read_review_package(directory)

    def test_crop_pixels_are_checked_even_after_manifest_resealed(self) -> None:
        region = PixelRegion(left=0, top=0, right=16, bottom=12)
        request = review_request().model_copy(update={"scope": "local", "target_kind": "feature", "reference_region": region})
        source = self.capture(request=request)
        directory = write_review_package(self.root / "report", source, matched_review(source.criteria))
        (directory / "views/reference.png").write_bytes(png("green"))
        self.reseal(directory)
        with self.assertRaisesRegex(ValueError, "局部视图"):
            read_review_package(directory)

    def test_path_escape_symlink_duplicate_json_and_nonfinite_are_rejected(self) -> None:
        directory = self.publish()
        manifest_path = directory / "manifest.json"
        original = manifest_path.read_bytes()
        manifest = json.loads(original)
        manifest["files"]["../outside.png"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_review_package(directory)
        manifest_path.write_bytes(original)
        path = directory / "reference.png"
        path.unlink()
        path.symlink_to(self.reference)
        with self.assertRaisesRegex(ValueError, "路径"):
            read_review_package(directory)
        path.unlink()
        path.write_bytes(self.reference.read_bytes())
        manifest_path.write_text('{"schema_version":"visual_review_v1","schema_version":"visual_review_v1"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重复"):
            read_review_package(directory)
        manifest_path.write_text('{"content_sha256":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不允许"):
            read_review_package(directory)

    def test_existing_target_and_failed_publish_preserve_inputs(self) -> None:
        directory = self.publish()
        old = (directory / "manifest.json").read_bytes()
        source = self.capture("another")
        with self.assertRaises(ValueError):
            write_review_package(directory, source, matched_review(source.criteria))
        self.assertEqual((directory / "manifest.json").read_bytes(), old)
        with (
            patch("shader_deep.infrastructure.storage.review_package.read_review_package", side_effect=OSError("read failure")),
            self.assertRaises(OSError),
        ):
            write_review_package(self.root / "failed", source, matched_review(source.criteria))
        self.assertFalse((self.root / "failed").exists())
        self.assertTrue((source.directory / "inputs/reference.png").is_file())
        self.assertEqual(list(self.root.glob(".failed-*")), [])
