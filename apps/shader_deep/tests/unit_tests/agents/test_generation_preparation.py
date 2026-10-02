"""验证报告准备能登记固定任务并交付后续上下文所需材料."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from shader_deep.agents.generation.context import _build_generation_context
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from shader_deep.workflows.generation_from_report import _prepare_generation
from tests.unit_tests.agents.test_context import BASE_CODE, fixture_state, metadata
from tests.unit_tests.infrastructure.test_report_package import package_libraries


class GenerationPreparationTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = fixture_state(self.root)
        image_path = self.root / "source.png"
        Image.new("RGBA", (24, 18), (255, 160, 200, 255)).save(image_path)
        reference = image_path.read_bytes()
        manifest = ReportManifest(reference=ReferenceAsset(sha256=hashlib.sha256(reference).hexdigest()), target_element_id="E1", status="completed")
        self.report = write_report_package(self.root / "report", package_libraries(), manifest, reference)
        # 将默认输出位置限定到测试目录, 仍由真实目录分配函数生成独立 run-*.
        allocator = patch(
            "shader_deep.workflows.generation_from_report.create_run_directory",
            side_effect=lambda parent: create_run_directory(parent if parent is not None else self.root / "runs"),
        )
        allocator.start()
        self.addCleanup(allocator.stop)

    def test_prepare_registers_new_task_preserves_baseline_and_saves_input_mapping(self) -> None:
        prepared = _prepare_generation(
            self.report,
            "S1",
            "只复现粉色圆形",
            background="透明",
            alternative=0,
            state=self.state,
            baseline_id="B7",
            asset_root=self.root,
        )
        task = prepared.state["tasks"][prepared.task_id]
        target = prepared.state["targets"][task.target_version]
        self.assertNotIn(task.id, self.state["tasks"])
        self.assertNotIn(target.version, self.state["targets"])
        self.assertEqual(prepared.state["candidates"]["B7"], self.state["candidates"]["B7"])
        self.assertEqual(task.baseline_id, "B7")
        self.assertEqual((prepared.options.width, prepared.options.height), (24, 18))
        self.assertIn("背景要求: 透明", target.constraints)
        context = _build_generation_context(prepared.state, prepared.task_id, asset_root=prepared.asset_root, inputs=prepared.inputs)
        self.assertEqual(metadata(context)["generation_scheme"]["selected"]["choices"], {"F1": ["M2"]})
        self.assertEqual(metadata(context)["candidates"][0]["code"], BASE_CODE)
        record = json.loads((prepared.run_dir / "run.json").read_text())
        binding = record["blackboard"]["tasks"][task.id]["generation_binding"]
        self.assertEqual(binding["content_sha256"], prepared.inputs.binding.content_sha256)
        self.assertEqual(binding["alternative"], 0)
        self.assertEqual(record["phase"], "prepared")
        self.assertEqual(record["inputs"]["baseline"]["candidate_id"], "B7")
        self.assertEqual(Path(record["inputs"]["baseline"]["code_path"]).read_text(), BASE_CODE)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 1)

    def test_explicit_render_configuration_and_no_baseline(self) -> None:
        options = GenerationOptions(width=32, height=20, time=1.5, max_attempts=2, output_dir=self.root / "custom")
        prepared = _prepare_generation(self.report, "S1", "复现圆形", background="白色", options=options)
        self.assertEqual(prepared.options, options)
        self.assertEqual(prepared.run_dir.parent, options.output_dir)
        self.assertIsNone(prepared.state["tasks"][prepared.task_id].baseline_id)
        self.assertIsNone(prepared.inputs.baseline)

    def test_invalid_input_does_not_publish_a_prepared_run_or_change_existing_records(self) -> None:
        for changes in ({"baseline_id": "missing"}, {"sketch_id": "missing"}, {"background": " "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _prepare_generation(
                    **{"report_dir": self.report, "sketch_id": "S1", "request": "复现圆形", "background": "白色", "state": self.state, **changes}
                )
        self.assertEqual(list((self.root / "runs").glob("run-*")), [])
        self.assertEqual(set(self.state["tasks"]), {"G0", "G1"})
        self.assertEqual(self.state["candidates"]["B7"].code_path, "baseline.glsl")
