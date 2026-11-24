"""从原运行捕获与接受快照恢复 V2 报告, 验证身份、并发和路径边界."""

from __future__ import annotations

import errno
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.domain.visual_review import PixelRegion, RequiredRegion, ReviewCoverage
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.workflows.review import ReviewOptions, run_review
from shader_deep.workflows.review.publication import _atomic_json, publication_lock, republish_review
from tests.unit_tests.fixtures._review_fixture import review_request


class ReviewPublicationTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.image = self.root / "image.png"
        with Image.new("RGB", (8, 8), "green") as image:
            image.save(self.image)
        self.options = ReviewOptions(output_dir=self.root / "runs")
        frame = PixelRegion(left=0, top=0, right=8, bottom=8)
        absent = RequiredRegion(dimension="shape", check="符合参考表现", region=PixelRegion(left=8, top=0, right=16, bottom=8))
        coverage = ReviewCoverage(reference_window=frame, rendered_window=frame, required_regions=(absent,))
        self.request = review_request().model_copy(update={"protocol": "visual_review_v2", "coverage": coverage})

    def fail_publication(self) -> Path:
        with patch("shader_deep.workflows.review.publication.write_review_package", side_effect=OSError("disk full")):
            outcome = run_review(self.image, self.image, self.request, options=self.options)
        self.assertEqual((outcome.status, outcome.report_dir, outcome.verdict), ("failed", None, None))
        self.assertIn("disk full", outcome.error)
        return outcome.run_dir

    def test_concurrent_recovery_restores_original_report_then_repairs_index(self) -> None:
        directory = self.fail_publication()
        original = json.loads((directory / "result.json").read_bytes())
        self.image.unlink()
        with patch("shader_deep.workflows.review.entry.build_model") as model, ThreadPoolExecutor(max_workers=3) as pool:
            copies = list(pool.map(republish_review, [directory] * 3))
        model.assert_not_called()
        package = read_review_package(directory / "report")
        for result in copies:
            self.assertEqual((result.status, result.verdict, result.action_summary), ("completed", "inconclusive", package.report.action_summary))
            self.assertEqual(result.execution, original["execution"])
        self.assertEqual(package.report.check_answers, ())
        saved = (directory / "result.json").read_bytes()
        self.assertEqual(json.loads(saved)["options"], original["options"])
        (directory / "result.json").unlink()
        with patch("shader_deep.workflows.review.publication.write_review_package") as writer:
            republish_review(directory)
        writer.assert_not_called()
        self.assertEqual((directory / "result.json").read_bytes(), saved)
        self.assertEqual(read_review_package(directory / "report").content_sha256, package.content_sha256)

    def test_conflicting_valid_package_is_never_replaced(self) -> None:
        directory = self.fail_publication()
        other = run_review(self.image, self.image, self.request, options=self.options)
        shutil.copytree(other.report_dir, directory / "report")
        existing = read_review_package(directory / "report")
        index = (directory / "result.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "报告.*冲突"):
            republish_review(directory)
        self.assertEqual((directory / "result.json").read_bytes(), index)
        self.assertEqual(read_review_package(directory / "report").content_sha256, existing.content_sha256)

    def test_first_publication_and_recovery_without_hard_links(self) -> None:
        with patch("shader_deep.workflows.review.publication._save_receipt", side_effect=OSError("disk full")):
            failed = run_review(self.image, self.image, self.request, options=self.options)
        with patch("os.link", side_effect=OSError(errno.ENOTSUP, "hard links unavailable")):
            first = run_review(self.image, self.image, self.request, options=self.options)
            restored = republish_review(failed.run_dir)
            for outcome in (first, restored):
                with self.subTest(directory=outcome.run_dir):
                    self.assertEqual(outcome.status, "completed", outcome.error)
                    receipt = outcome.run_dir / "results/publication.json"
                    original = receipt.read_bytes()
                    with publication_lock(outcome.run_dir), self.assertRaises(FileExistsError):
                        _atomic_json(receipt, {"unexpected": True}, create=True)
                    self.assertEqual(republish_review(outcome.run_dir).verdict, "inconclusive")
                    self.assertEqual(receipt.read_bytes(), original)

    def test_failure_to_save_first_receipt_can_recover_from_acceptance_alone(self) -> None:
        with patch("shader_deep.workflows.review.publication._save_receipt", side_effect=OSError("receipt disk full")):
            failed = run_review(self.image, self.image, self.request, options=self.options)
        directory = failed.run_dir
        self.assertEqual(failed.status, "failed")
        self.assertIn("receipt disk full", json.loads((directory / "result.json").read_bytes())["error"])
        self.assertFalse((directory / "results/publication.json").exists())
        accepted_path = directory / "results/accepted.json"
        original = accepted_path.read_bytes()
        for section, key in (("publication_seed", "run_id"), ("submission", "summary")):
            changed = json.loads(original)
            changed[section][key] = "其他运行"
            accepted_path.write_text(json.dumps(changed))
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "接受快照"):
                republish_review(directory)
            self.assertFalse((directory / "report").exists())
        accepted_path.write_bytes(original)
        with patch("shader_deep.workflows.review.entry.build_model") as model, ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(republish_review, [directory] * 3))
        model.assert_not_called()
        self.assertEqual({item.verdict for item in outcomes}, {"inconclusive"})
        self.assertEqual(outcomes[0].execution["usage_scope"], "unknown_at_acceptance")
        self.assertIsNone(outcomes[0].execution["elapsed_seconds"])
        self.assertEqual(outcomes[0].execution["model_calls"], failed.execution["model_calls"])
        self.assertTrue((directory / "results/publication.json").is_file())

    def test_changed_input_or_accepted_bytes_are_rejected_before_publication(self) -> None:
        directory = self.fail_publication()
        for name in ("inputs/task.json", "results/accepted.json"):
            path = directory / name
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "凭据"):
                republish_review(directory)
            path.write_bytes(original)
        self.assertFalse((directory / "report").exists())
        accepted = directory / "results/accepted.json"
        legacy = json.loads(accepted.read_bytes())
        del legacy["publication_seed"], legacy["publication_seed_sha256"]
        accepted.unlink()
        with self.assertRaisesRegex(ValueError, "缺少成功接受"):
            republish_review(directory)
        (directory / "results/publication.json").unlink()
        accepted.write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError, "缺少.*V2 发布凭据"):
            republish_review(directory)

    def test_symlinked_artifacts_and_run_alias_cannot_redirect_recovery(self) -> None:
        directory = self.fail_publication()
        alias = self.root / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            republish_review(alias)
        for name in ("results/accepted.json", "inputs/reference.png", "results/publication.json", "result.json"):
            path = directory / name
            outside = self.root / path.name
            path.rename(outside)
            original = outside.read_bytes()
            path.symlink_to(outside)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "路径"):
                republish_review(directory)
            self.assertEqual(outside.read_bytes(), original)
            path.unlink()
            outside.rename(path)
