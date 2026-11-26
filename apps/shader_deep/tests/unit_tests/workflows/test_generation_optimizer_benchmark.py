"""用固定规划、真实 PNG 和离线生成结果核对两组实验报告."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from PIL import Image

from scripts import benchmark_generation_optimizer as benchmark
from shader_deep.domain.blackboard import add_candidate
from shader_deep.domain.tasks import CandidateRecord
from shader_deep.infrastructure.storage.artifacts import save_run
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from tests.unit_tests.fixtures._planning_fixture import planning_report

if TYPE_CHECKING:
    from shader_deep.agents.generation.options import GenerationOptions
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs


class GenerationOptimizerBenchmarkTests(unittest.TestCase):
    def test_equal_inputs_and_budgets_actual_selected_roi_and_missing_usage(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        packages = [planning_report(root / str(i), color=(10, 20, value, 255)) for i, value in enumerate((30, 48))]
        cases = [benchmark._case(str(path), "S1", "4,4,5,5") for path in packages]
        conditions = []
        model = Mock(model_name="local-fixture")

        def generate(
            state: BlackboardState,
            _task: str,
            _root: Path,
            options: GenerationOptions,
            *,
            run_dir: Path,
            inputs: CapturedPlanningInputs,
            **kwargs: object,
        ) -> None:
            conditions.append((inputs.binding.content_sha256, options, kwargs["model"]))
            is_second_case = inputs.binding.content_sha256 == cases[1].package.content_sha256
            if is_second_case and not options.optimize_parameters:
                save_run(run_dir, state, {"stop_reason": "blocked", "attempts": 1, "model_calls": 2})
                return
            preview = run_dir / "candidate.png"
            with Image.open(inputs.reference_path) as image:
                image.putpixel((0, 0), (255, 0, 51, 255))
                if not options.optimize_parameters:
                    image.putpixel((4, 4), (90, 20, 30, 255))
                image.save(preview)
            candidate = CandidateRecord(id="C1", task_id="G1", code_path=str(run_dir / "shader.glsl"), preview_path=str(preview))
            usage = None if is_second_case else {"input_tokens": 21, "output_tokens": 9, "total_tokens": 30}
            save_run(
                run_dir,
                add_candidate(state, candidate),
                {
                    "stop_reason": "completed",
                    "selected_candidate_id": candidate.id,
                    "attempts": 1,
                    "model_calls": 2,
                    "token_usage": usage,
                },
            )
            if options.optimize_parameters:
                write_snapshot(run_dir / "optimization-002.json", {"status": "improved", "candidate_id": candidate.id})

        with patch.object(benchmark, "_run_session", side_effect=generate):
            report = json.loads(benchmark._compare(cases, model, root, attempts=3, calls=5).read_bytes())
        plain, optimized = report["cases"][0]["arms"]
        self.assertAlmostEqual(plain["pixel_mae"], 80 / (4 * 255))
        self.assertEqual(optimized["pixel_mae"], 0.0)
        self.assertTrue(optimized["selected_optimizer_candidate"])
        self.assertEqual(optimized["reported_tokens"]["total_tokens"], 30)
        failure, missing_usage = report["cases"][1]["arms"]
        self.assertEqual(failure["stop_reason"], "blocked")
        self.assertIsNone(failure["pixel_mae"])
        self.assertIsNone(missing_usage["reported_tokens"])
        for start, end in ((0, 1), (2, 3)):
            key, a, source = conditions[start]
            other_key, b, other_source = conditions[end]
            self.assertEqual((key, source, replace(a, optimize_parameters=True)), (other_key, other_source, b))
            self.assertEqual((a.optimize_parameters, b.optimize_parameters), (False, True))
