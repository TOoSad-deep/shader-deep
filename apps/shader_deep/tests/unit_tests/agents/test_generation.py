"""验证渲染反馈、候选选择、硬预算与 CLI 完成状态."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Self
from unittest.mock import Mock, patch

from langchain_core.tracers.context import collect_runs, tracing_v2_enabled
from langsmith import Client

from shader_deep import cli
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.rendering import RenderError
from shader_deep.workflows.generation import GenerationIncompleteError, generate_shader, generate_task, run_generation
from tests.unit_tests.agents.test_context import BASE_CODE, PNG
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, task_payloads, tool_results


class ThreadBoundRenderer:
    renders: ClassVar[list[tuple[int, int, float]]] = []
    closed: ClassVar[int] = 0

    def __enter__(self) -> Self:
        self.owner = threading.get_ident()
        return self

    def render(self, code: str, width: int, height: int, time: float) -> bytes:
        if self.owner != threading.get_ident():
            msg = "Renderer crossed threads"
            raise RuntimeError(msg)
        self.renders.append((width, height, time))
        if "unknown_symbol" in code:
            msg = "ERROR: 0:1: unknown_symbol is not declared"
            raise RenderError(msg, stage="fragment_compile")
        return PNG

    def close(self) -> None:
        if self.owner != threading.get_ident():
            msg = "Renderer closed on a different thread"
            raise RuntimeError(msg)
        type(self).closed += 1


class GenerationTests(GenerationFixture):
    def setUp(self) -> None:
        super().setUp()
        ThreadBoundRenderer.renders = []
        ThreadBoundRenderer.closed = 0
        renderer = patch("shader_deep.agents.generation.tools.render.WebGL2Renderer", ThreadBoundRenderer)
        renderer.start()
        self.addCleanup(renderer.stop)

    def test_traces_include_model_context_tools_and_artifact_metadata(self) -> None:
        client = Mock(spec=Client)
        with tracing_v2_enabled(client=client):
            outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        runs = [call.kwargs for call in client.create_run.call_args_list]
        root = next(run for run in runs if run["name"] == "shader-deep.generation")
        self.assertEqual(root["extra"]["metadata"]["run_dir"], str(outcome.run_dir))
        calls = [run for run in runs if run["run_type"] in {"llm", "tool"}]
        self.assertEqual({call["name"] for call in calls if call["run_type"] == "tool"}, {"render_shader", "finish_shader"})
        models = [call for call in calls if call["run_type"] == "llm"]
        self.assertEqual(len(models), 2)
        self.assertIn("data:image/png;base64,", str(models[-1]["inputs"]))
        self.assertIn("generation_task_context", str(models[-1]["inputs"]))
        self.assertGreater(str(models[-1]["inputs"]).count("data:image/png;base64,"), str(models[0]["inputs"]).count("data:image/png;base64,"))
        for call in calls:
            self.assertEqual(call["trace_id"], root["id"])
            self.assertEqual(call["extra"]["metadata"]["session_id"], outcome.run_dir.name)
            self.assertEqual(call["extra"]["metadata"]["task_id"], "G1")

    def test_reinvoked_agent_traces_share_a_session(self) -> None:
        self.response = lambda _request: {"role": "assistant", "content": "继续检查"}
        with collect_runs() as collector:
            outcome = run_generation(self.state, "G1", asset_root=self.root, options=replace(self.options, max_attempts=1))
        agents = [run for run in collector.traced_runs if run.name == "shader-deep.generation"]
        self.assertGreater(len(agents), 1)
        self.assertEqual({run.extra["metadata"]["session_id"] for run in agents}, {outcome.run_dir.name})

    def test_error_repair_preview_selection_and_artifacts(self) -> None:
        self.response = self.repair_then_finish
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.model_calls, 3)
        self.assertEqual(Path(outcome.selected_candidate.code_path).read_text(), BASE_CODE)
        self.assertEqual(Path(outcome.selected_candidate.preview_path).read_bytes(), PNG)
        self.assertIn("unknown_symbol", tool_results(self.requests[1])[0]["error"])
        self.assertEqual(len(task_payloads(self.requests[2])[0]["candidates"]), 3)
        self.assertEqual(task_payloads(self.requests[2])[0]["task"]["baseline_id"], "B7")
        self.assertEqual(len(self.state["candidates"]), 1)
        manifest = json.loads((outcome.run_dir / "run.json").read_text())
        self.assertEqual(manifest["selected_candidate_id"], outcome.selected_candidate.id)
        self.assertEqual(manifest["render"], {"width": 32, "height": 24, "time": 0.0})
        self.assertEqual(len(list(outcome.run_dir.glob("*.glsl"))), 2)
        self.assertEqual(ThreadBoundRenderer.closed, 1)

    def test_selected_code_is_returned_without_an_extra_model_call(self) -> None:
        code = generate_task(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(code, BASE_CODE)
        self.assertEqual(len(self.requests), 2)
        names = {item["function"]["name"] for item in self.requests[0]["tools"]}
        self.assertEqual(names, {"render_shader", "finish_shader"})

    def test_plain_text_cannot_bypass_rendering_and_stops_at_model_limit(self) -> None:
        self.response = lambda _request: {"role": "assistant", "content": BASE_CODE}
        options = replace(self.options, max_attempts=1)
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=options)
        self.assertIsNone(outcome.selected_candidate)
        self.assertEqual(outcome.stop_reason, "model_limit")
        self.assertEqual(outcome.model_calls, 3)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(ThreadBoundRenderer.renders, [])

    def test_render_attempt_limit_is_enforced_by_code(self) -> None:
        self.response = lambda _request: self.call("render_shader", {"glsl_code": BASE_CODE})
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=replace(self.options, max_attempts=1))
        self.assertIsNone(outcome.selected_candidate)
        self.assertEqual(outcome.stop_reason, "attempt_limit")
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(len(ThreadBoundRenderer.renders), 1)
        self.assertEqual(len(list(outcome.run_dir.glob("*.glsl"))), 1)

    def test_finish_in_same_batch_is_rejected_until_preview_is_presented(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            if not tool_results(request):
                directory = next(self.options.output_dir.iterdir())
                message = self.call("render_shader", {"glsl_code": BASE_CODE})
                finish = self.call("finish_shader", {"candidate_id": f"{directory.name}-c001", "assessment": "尚未收到预览"})
                finish["tool_calls"][0]["id"] = "call-blind-finish"
                message["tool_calls"].extend(finish["tool_calls"])
                return message
            self.assertTrue(any(result["status"] == "invalid_selection" for result in tool_results(request)))
            return self.render_then_finish(request)

        self.response = response
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertEqual(outcome.model_calls, 2)

    def test_hidden_builtin_tool_is_not_executed(self) -> None:
        def response(request: dict[str, object]) -> dict[str, object]:
            if not tool_results(request):
                return self.call("write_file", {"file_path": "/bypass.txt", "content": "not allowed"})
            return self.render_then_finish(request)

        self.response = response
        outcome = run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(outcome.stop_reason, "completed")
        self.assertIn("本轮只允许", tool_results(self.requests[1])[0]["message"])
        self.assertEqual(outcome.attempts, 1)

    def test_cli_delivers_rendered_code_and_reports_preview(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "shader-deep",
                    str(self.root / "reference.PNG"),
                    "生成粉色圆形",
                    "--width",
                    "64",
                    "--height",
                    "32",
                    "--time",
                    "1",
                    "--max-attempts",
                    "1",
                    "--output-dir",
                    str(self.root / "cli-runs"),
                ],
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main()
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), BASE_CODE + "\n")
        self.assertIn("Preview:", stderr.getvalue())
        self.assertEqual(ThreadBoundRenderer.renders, [(64, 32, 1.0)])

    def test_incomplete_cli_has_no_shader_stdout(self) -> None:
        self.response = lambda _request: {"role": "assistant", "content": BASE_CODE}
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "shader-deep",
                    str(self.root / "reference.PNG"),
                    "test",
                    "--max-attempts",
                    "1",
                    "--output-dir",
                    str(self.root / "cli-runs"),
                ],
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = cli.main()
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Generation incomplete: model_limit", stderr.getvalue())

    def test_legacy_string_api_exposes_incomplete_artifacts(self) -> None:
        self.response = lambda _request: {"role": "assistant", "content": "not rendered"}
        with self.assertRaises(GenerationIncompleteError) as caught:
            generate_shader(self.root / "reference.PNG", "test", options=replace(self.options, max_attempts=1))
        self.assertTrue((caught.exception.outcome.run_dir / "run.json").is_file())

    def test_invalid_input_and_options_fail_before_model_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "Prompt must not be empty"):
            generate_shader(self.root / "reference.PNG", "  ", options=self.options)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            GenerationOptions(max_attempts=0)
        (self.root / "baseline.glsl").unlink()
        with self.assertRaises(FileNotFoundError):
            run_generation(self.state, "G1", asset_root=self.root, options=self.options)
        self.assertEqual(self.requests, [])
