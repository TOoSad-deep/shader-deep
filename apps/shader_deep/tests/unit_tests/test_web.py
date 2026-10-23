"""本地页面任务的输入、交接、终态和文件边界, 不调用真实模型."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image
from pydantic import ValidationError

from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from shader_deep.web.jobs import Job, Jobs, Submission, _command
from tests.unit_tests.infrastructure.test_report_package import package_libraries


def png_bytes() -> bytes:
    with io.BytesIO() as stream:
        Image.new("RGB", (8, 8), "purple").save(stream, format="PNG")
        return stream.getvalue()


class WebJobsTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.jobs = Jobs(self.root / "jobs")
        self.addCleanup(self.jobs.close)
        self.image = base64.b64encode(png_bytes()).decode()

    def job(self, mode: str = "generate", *, code: int | None = 0) -> Job:
        root = self.root / "job"
        root.mkdir(exist_ok=True)
        process = Mock(spec=subprocess.Popen)
        process.poll.return_value = code
        request = Submission.model_validate({"mode": mode, "prompt": "测试"})
        job = Job("job", root, request, process)
        self.jobs.items[job.identity] = job
        return job

    def test_invalid_image_and_numeric_fields_rejected_before_start(self) -> None:
        with patch("shader_deep.web.jobs.subprocess.Popen") as start:
            for image in ("not base64", base64.b64encode(b"not a png").decode()):
                with self.subTest(image=image), self.assertRaises(ValueError):
                    self.jobs.start(Submission(mode="generate", prompt="生成", image=image))
            start.assert_not_called()
        for values in ({"width": 0}, {"attempts": True}, {"perspectives": 4}, {"prompt": "  "}, {"mode": "shell"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Submission.model_validate({"mode": "generate", "prompt": "生成", **values})

    def test_upload_and_child_output_survive_process_exit(self) -> None:
        command = [sys.executable, "-c", "import sys; sys.stdout.write('glsl'); sys.stderr.write('progress')"]
        with patch("shader_deep.web.jobs._command", return_value=command):
            job = self.jobs.start(Submission(mode="generate", prompt="生成", image=self.image))
        job.process.wait(timeout=10)
        self.assertEqual(job.status(), "completed")
        self.assertEqual((job.root / "reference.png").read_bytes(), png_bytes())
        self.assertEqual(job.snapshot()["log"], "progress")
        self.assertEqual((job.root / "stdout.txt").read_text(), "glsl")
        self.assertEqual(job.request.image, "")

    def test_oversized_png_and_decompression_warning_are_rejected(self) -> None:
        with io.BytesIO() as stream:
            Image.new("RGB", (4097, 1)).save(stream, format="PNG")
            oversized = base64.b64encode(stream.getvalue()).decode()
        with self.assertRaisesRegex(ValueError, "4096"):
            self.jobs.start(Submission(mode="generate", prompt="生成", image=oversized))
        with patch("shader_deep.web.jobs.Image.open", side_effect=Image.DecompressionBombWarning), self.assertRaises(ValueError):
            self.jobs.start(Submission(mode="generate", prompt="生成", image=self.image))

    def test_single_active_job_and_cancel_reaps_process(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        request = Submission(mode="generate", prompt="生成", image=self.image)
        with patch("shader_deep.web.jobs._command", return_value=command):
            job = self.jobs.start(request)
            with self.assertRaisesRegex(ValueError, "已有任务"):
                self.jobs.start(request)
        job.stop()
        self.assertEqual(job.status(), "cancelled")
        self.assertIsNotNone(job.process.poll())
        self.assertTrue((job.root / "reference.png").is_file())

    def test_shutdown_rejects_a_late_submission(self) -> None:
        self.jobs.close()
        with self.assertRaisesRegex(ValueError, "正在关闭"):
            self.jobs.start(Submission(mode="generate", prompt="生成", image=self.image))

    def test_failed_generation_keeps_business_stop_reason_and_candidate(self) -> None:
        job = self.job(code=1)
        run = job.root / "output" / "run-example"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({"stop_reason": "attempt_limit", "selected_candidate_id": None}))
        (run / "candidate-001.png").write_bytes(png_bytes())
        self.assertEqual(job.snapshot()["status"], "attempt_limit")
        self.assertIsNone(job.details()["selected_candidate_id"])
        self.assertIn("candidate-001.png", json.dumps(job.snapshot()["files"]))

    def test_download_rejects_traversal_and_external_symlinks(self) -> None:
        job = self.job()
        secret = self.root / "secret.txt"
        secret.write_text("private")
        (job.root / "link.txt").symlink_to(secret)
        for name in ("../secret.txt", str(secret), "link.txt", ".env"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                job.file(name)
        self.assertNotIn("link.txt", json.dumps(job.snapshot()["files"]))

    def publish_report(self) -> Job:
        job = self.job("analyze", code=1)
        report = job.root / "output" / "run-analysis" / "report"
        manifest = ReportManifest(
            reference=ReferenceAsset(sha256=hashlib.sha256(png_bytes()).hexdigest()), target_element_id="E1", status="completed"
        )
        write_report_package(report, package_libraries(), manifest, png_bytes())
        (job.root / "stdout.txt").write_text(json.dumps({"status": "partial", "report_dir": str(report)}))
        return job

    def test_partial_report_is_visible_and_explicit_selection_is_validated(self) -> None:
        source = self.publish_report()
        self.assertEqual(source.status(), "partial")
        self.assertIn("二维分层", str(source.snapshot()["sketches"]))
        request = Submission(mode="report", prompt="只实现中央元素", source=source.identity, sketch="S1", alternative=0)
        with patch("shader_deep.web.jobs._command", return_value=[sys.executable, "-c", "pass"]) as command:
            generated = self.jobs.start(request)
        generated.process.wait(timeout=10)
        self.assertEqual(command.call_args.args[2], source.report())
        self.assertEqual((generated.root / "reference.png").read_bytes(), png_bytes())
        for fields in ({"sketch": "missing"}, {"alternative": 99}, {"source": "../outside"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.jobs.start(request.model_copy(update=fields))

    def test_shell_characters_remain_a_single_cli_argument(self) -> None:
        prompt = "画出 $(touch /tmp/should-not-exist); `whoami`"
        request = Submission(mode="generate", prompt=prompt)
        command = _command(request, self.root, None)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[3], "shader_deep.cli.generation")
        self.assertIn(prompt, command)

    def test_cli_selection_and_options_match_report_request(self) -> None:
        request = Submission(mode="report", prompt="生成", source="job", sketch="S2", alternative=0, background="深灰", width=768, height=384)
        command = _command(request, self.root, self.root / "report")
        for flag, value in (("--sketch", "S2"), ("--alternative", "0"), ("--background", "深灰"), ("--width", "768"), ("--height", "384")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn(str(self.root / "reference.png"), command)
