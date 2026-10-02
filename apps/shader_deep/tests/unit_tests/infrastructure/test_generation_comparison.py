"""验证人工选择来自实际子运行, 比较索引不改写原产物."""

from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import unquote

from shader_deep.domain.generation_comparison import ComparisonItem, GenerationComparison, GenerationPlan
from shader_deep.infrastructure.storage.generation_comparison import (
    read_generation_comparison,
    select_generation_comparison,
    write_generation_comparison,
)
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from tests.unit_tests.agents.test_context import BASE_CODE, PNG


class GenerationComparisonStorageTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "comparison root"
        completed, blocked = self.root / "first run", self.root / "second run"
        completed.mkdir()
        blocked.mkdir()
        code, preview = completed / "candidate.glsl", completed / "candidate.png"
        code.write_text(BASE_CODE, encoding="utf-8")
        preview.write_bytes(PNG)
        write_snapshot(
            completed / "run.json",
            {
                "task_id": "G1",
                "stop_reason": "completed",
                "selected_candidate_id": "C1",
                "blackboard": {"candidates": {"C1": {"id": "C1", "task_id": "G1", "code_path": str(code), "preview_path": str(preview)}}},
            },
        )
        write_snapshot(blocked / "run.json", {"task_id": "G1", "stop_reason": "blocked", "selected_candidate_id": None})
        self.record = GenerationComparison(
            directory=str(self.directory),
            report_sha256="a" * 64,
            reference_sha256="b" * 64,
            element_id="E1",
            request="复现同一圆形",
            background="透明",
            baseline_id=None,
            model_name="fixture-model",
            width=32,
            height=24,
            time=0.0,
            max_attempts=2,
            items=(
                ComparisonItem(
                    plan=GenerationPlan(name="曲线遮罩", sketch_id="S1"),
                    run_dir=str(completed),
                    task_id="G1",
                    stop_reason="completed",
                    selected_candidate_id="C1",
                    code_path=str(code),
                    preview_path=str(preview),
                ),
                ComparisonItem(
                    plan=GenerationPlan(name="高斯遮罩", sketch_id="S1", alternative=0), run_dir=str(blocked), task_id="G1", stop_reason="blocked"
                ),
            ),
        )

    def test_selection_round_trip_keeps_child_artifacts_and_links(self) -> None:
        destination = write_generation_comparison(self.record)
        self.assertIsNone(read_generation_comparison(self.directory).selected_item)
        children = {path: path.read_bytes() for item in self.record.items for path in Path(item.run_dir).iterdir()}
        for link in re.findall(r"\]\(([^)]+)\)", (self.directory / "README.md").read_text(encoding="utf-8")):
            self.assertFalse(Path(unquote(link)).is_absolute())
            self.assertTrue((self.directory / unquote(link)).is_file())
        # 索引中的旧位置不能将用户这次更新重定向到另一目录.
        payload = json.loads(destination.read_text(encoding="utf-8"))
        payload["directory"] = str(self.root / "elsewhere")
        write_snapshot(destination, payload)
        selected = select_generation_comparison(self.directory, 0, "C1", reason="边缘更接近参考")
        self.assertEqual(read_generation_comparison(self.directory), selected)
        self.assertEqual(selected.selected_item, 0)
        self.assertEqual(selected.directory, str(self.directory))
        self.assertIn("边缘更接近参考", (self.directory / "README.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "elsewhere").exists())
        self.assertEqual({path: path.read_bytes() for path in children}, children)

    def test_invalid_or_stale_selection_does_not_update_comparison(self) -> None:
        destination = write_generation_comparison(self.record)
        before = destination.read_bytes()
        for index, candidate in ((0, "wrong-candidate"), (1, "C1"), (2, "C1")):
            with self.subTest(index=index, candidate=candidate), self.assertRaises(ValueError):
                select_generation_comparison(self.directory, index, candidate)
            self.assertEqual(destination.read_bytes(), before)
        child = Path(self.record.items[0].run_dir) / "run.json"
        payload = json.loads(child.read_text(encoding="utf-8"))
        payload["blackboard"]["candidates"]["C1"]["task_id"] = "another-task"
        write_snapshot(child, payload)
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            select_generation_comparison(self.directory, 0, "C1")
        self.assertEqual(destination.read_bytes(), before)
        self.assertIsNone(read_generation_comparison(self.directory).selected_item)
