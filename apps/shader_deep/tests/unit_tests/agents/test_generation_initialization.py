"""验证视觉记忆初始化失败时保留可追溯制品并释放运行资源."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

from shader_deep.infrastructure.storage.visual_memory import MaterialError
from shader_deep.workflows.generation import run_generation
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture


class GenerationInitializationTests(GenerationFixture):
    def test_oversized_baseline_preserves_failure_record_before_starting_worker(self) -> None:
        code = "x" * 32_769
        (self.root / "baseline.glsl").write_text(code, encoding="utf-8")
        with self.assertRaises(MaterialError) as caught:
            run_generation(self.state, "G1", asset_root=self.root, options=replace(self.options, visual_memory=True))
        self.assertEqual(self.requests, [])
        directory = next(self.options.output_dir.iterdir())
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(record["stop_reason"], "error")
        self.assertEqual(record["model_calls"], 0)
        self.assertEqual(record["attempts"], 0)
        self.assertIn("MaterialError", record["error"])
        self.assertIn("32769", record["error"])
        self.assertIn(str(directory), " ".join(caught.exception.__notes__))
        index = json.loads((directory / "memory" / "index.json").read_text(encoding="utf-8"))
        baseline = next(item for item in index["materials"].values() if item["kind"] == "code")
        self.assertEqual((directory / baseline["path"]).read_text(encoding="utf-8"), code)
        self.assertFalse(any(thread.name.startswith("shader-render") for thread in threading.enumerate()))
