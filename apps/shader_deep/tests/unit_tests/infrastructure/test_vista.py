"""验证 VISTA 内容寻址发布在硬链接不可用时仍保持一致."""

from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shader_deep.infrastructure.storage.vista import publish_image, read_image


class VistaPublicationTests(TestCase):
    def test_atomic_fallback_publishes_same_record_for_concurrent_writers(self) -> None:
        png, code, render = b"image", b"mainImage", {"time": 0.5, "uniforms": {"uX": 0.3}}
        with TemporaryDirectory() as directory, patch("shader_deep.infrastructure.storage.vista.os.link", side_effect=OSError(errno.EXDEV, "")):
            root = Path(directory)

            def publish() -> str:
                return publish_image(root, label="candidate", png=png, code=code, render=render)

            with ThreadPoolExecutor(max_workers=4) as workers:
                records = {job.result() for job in [workers.submit(publish) for _ in range(4)]}
            self.assertEqual(len(records), 1)
            self.assertEqual(read_image(root, records.pop()), (png, code, render))
            self.assertEqual({folder.name: len(list(folder.iterdir())) for folder in root.iterdir()}, {"png": 1, "glsl": 1, "json": 1})

    def test_fallback_rejects_an_existing_corrupt_file(self) -> None:
        with TemporaryDirectory() as directory, patch("shader_deep.infrastructure.storage.vista.os.link", side_effect=OSError(errno.EPERM, "")):
            root = Path(directory)
            publish_image(root, label="candidate", png=b"original")
            artifact = next((root / "png").iterdir())
            artifact.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "archive content"):
                publish_image(root, label="candidate", png=b"original")
            self.assertEqual(artifact.read_bytes(), b"modified")
