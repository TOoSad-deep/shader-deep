"""验证整体入口经真实角色图、WebGL2、人工回滚与交付的接线."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from shader_deep.domain.visual_review import DIMENSION_IDS
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.workflows.optimization_workflow import GateADecision, GateBDecision, advance_workflow, read_workflow, start_workflow
from tests.unit_tests.domain.test_scene_observations import scene_submission
from tests.unit_tests.fixtures._generation_fixture import GenerationFixture, tool_results
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.workflows.test_upstream_review_criteria import scene_task


class WorkflowRoundTripTests(GenerationFixture):
    def _drafts(self, plan: dict) -> dict:
        return {
            "save_observation_draft": ("analysis", "/draft/observations.json", scene_submission().model_dump(mode="json")),
            "save_planning_draft": ("planning", "/draft/planning.json", plan),
        }

    def _upstream_reply(self, drafts: dict, names: set, request: dict) -> dict | None:
        for save, (role, path, report) in drafts.items():
            if save in names:
                calls = [call["function"]["name"] for msg in request["messages"] for call in msg.get("tool_calls", [])]
                if save not in calls:
                    return self.call(save, {"report": report})
                return self.call("finish_" + role, {}) if "read_file" in calls else self.call("read_file", {"file_path": path})
        return None

    def test_two_native_rounds_restore_and_deliver_the_reviewed_first_candidate(self) -> None:
        package = read_planning_package(planning_report(self.root / "material"))
        drafts = self._drafts(package.report.model_dump(mode="json"))
        reviewed = []

        def fixture(request: dict) -> dict:
            names = {tool["function"]["name"] for tool in request["tools"]}
            if upstream := self._upstream_reply(drafts, names, request):
                return upstream
            if "SceneCriteriaDraft" in names:
                return self.call("SceneCriteriaDraft", {"dimensions": [d.model_dump(mode="json") for d in scene_task().dimensions]})
            if "finish_review" in names:
                content = request["messages"][-1]["content"]
                evidence = json.loads(content[0]["text"])
                answers = [{"check_id": row["check_id"], "rationale": "固定双图供本地流程回放"} for row in evidence["answerable_checks"]]
                if reviewed:
                    answers[0]["findings"] = [
                        {
                            "region": "全图",
                            "reference_observation": "灰蓝底色",
                            "rendered_observation": "全黑",
                            "severity": "major",
                            "impact": "缺少色场",
                        }
                    ]
                reviewed.append(evidence)
                return self.call("finish_review", {"answers": answers, "summary": "本地回放评审"})
            if "DiagnosisSubmission" in names:
                data = json.loads(request["messages"][-1]["content"])
                self.assertTrue(data["visual_memory_history"])
                self.assertIn("vec4(0.", data["implementation"]["glsl"])
                items = [
                    {"issue_id": key, "source_ref": ref, "route": "code_structure", "target": "恢复基础色场", "evidence": ["该帧只输出黑色"]}
                    for key, ref in data["issues_to_diagnose"].items()
                ]
                return self.call("DiagnosisSubmission", {"items": items})
            message = self.render_then_finish(request)
            if reviewed and message["tool_calls"][0]["function"]["name"] == "render_shader":
                message = self.call("render_shader", {"glsl_code": "void mainImage(out vec4 c, in vec2 p) { c=vec4(0., 0., 0., 1.); }"})
            return message

        self.response = fixture
        state = start_workflow(package.source.reference_path, "本地固定图对照", output_dir=self.root / "workflows", max_output_tokens=1000)
        self.assertEqual(state.phase, "gate_a", state.reason)
        root = Path(state.directory)
        first = advance_workflow(root, GateADecision(action="generate_scene", sketch_id="S1"))
        self.assertIsNotNone(first.current_candidate, first.reason)
        self.assertEqual(first.current_candidate.verdict, "no_major_issue_observed", first.reason)
        revised = advance_workflow(root, GateBDecision(action="reroute", route="code_structure", reason="再生成一轮并核对回滚"))
        self.assertIsNotNone(revised.current_candidate, revised.reason)
        self.assertNotEqual(revised.current_candidate.id, first.current_candidate.id)
        self.assertNotEqual(revised.current_candidate.preview_sha256, first.current_candidate.preview_sha256)
        self.assertTrue(revised.current_candidate.major_issues, revised.reason)
        self.assertTrue(revised.current_candidate.regressions)
        card = json.loads((root / revised.current_candidate.diagnosis_path).read_bytes())
        self.assertEqual(card["items"][0]["route"], "code_structure")
        restored = advance_workflow(root, GateBDecision(action="rollback", candidate_id=first.current_candidate.id))
        self.assertEqual(restored.current_candidate.id, first.current_candidate.id)
        approved = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual(approved, read_workflow(root))
        self.assertEqual(approved.phase, "completed", approved.reason)
        self.assertEqual((root / approved.delivery_path / "preview.png").read_bytes(), (root / first.current_candidate.preview_path).read_bytes())
        self.assertEqual(approved.reported_output_tokens, len(self.requests))
        self.assertEqual(approved.request_attempts, len(self.requests))
        self.assertEqual(approved.remaining_output_tokens, 1000 - len(self.requests))
        trace = [json.loads(line) for line in (root / "workflow-trace.jsonl").read_text().splitlines()]
        self.assertEqual([row["action"] for row in trace if row["event"] == "gate_submitted"], ["generate_scene", "reroute", "rollback", "accept"])

    def test_diagnosis_tunes_unbound_uniform_and_protects_the_matched_pixels(self) -> None:
        package = read_planning_package(planning_report(self.root / "material"))
        drafts = self._drafts(package.report.model_dump(mode="json"))
        checks = [
            {"check": "左侧红色分量", "source_ref": "F8", "region": {"left": 0, "top": 0, "right": 16, "bottom": 24}, "tolerance": None},
            {"check": "右侧底色", "source_ref": "F15", "region": {"left": 16, "top": 0, "right": 32, "bottom": 24}, "tolerance": None},
        ]
        glsl = """
        uniform float uRed;
        void mainImage(out vec4 color, in vec2 p) {
            color = vec4(p.x < 16. ? uRed : 160. / 255., 180. / 255., 210. / 255., 1.);
        }
        """
        reviews = []

        def review(request: dict) -> dict:
            evidence = json.loads(request["messages"][-1]["content"][0]["text"])
            answers = [{"check_id": row["check_id"], "rationale": "本地测试检查指定的左右像素区域"} for row in evidence["answerable_checks"]]
            if not reviews:
                answers[0]["findings"] = [
                    {
                        "region": "左半侧",
                        "reference_observation": "目标红色分量为 160",
                        "rendered_observation": "实际红色分量为 51",
                        "severity": "major",
                        "impact": "左侧偏蓝",
                    }
                ]
            reviews.append(evidence)
            return self.call("finish_review", {"answers": answers, "summary": "定向调参测试"})

        def diagnose(request: dict) -> dict:
            data = json.loads(request["messages"][-1]["content"])
            self.assertIsNone(data["implementation"]["parameter_specs"][0]["target_ref"])
            self.assertTrue(any(row["id"] == "M3" and row["method"] for row in data["available_mechanisms"]))
            return self.call(
                "DiagnosisSubmission",
                {
                    "items": [
                        {
                            "issue_id": key,
                            "source_ref": ref,
                            "route": "numeric_tuning",
                            "target": "恢复左侧红色分量",
                            "evidence": ["uRed 只参与左半区域着色"],
                            "parameter_names": ["uRed"],
                        }
                        for key, ref in data["issues_to_diagnose"].items()
                    ]
                },
            )

        def fixture(request: dict) -> dict:
            names = {tool["function"]["name"] for tool in request["tools"]}
            if upstream := self._upstream_reply(drafts, names, request):
                return upstream
            if "SceneCriteriaDraft" in names:
                fields = {identity: {"reason": "本例只核对左右颜色"} for identity in DIMENSION_IDS if identity != "color"}
                return self.call("SceneCriteriaDraft", {**fields, "color": {"checks": checks}})
            if "finish_review" in names:
                return review(request)
            if "DiagnosisSubmission" in names:
                return diagnose(request)
            if tool_results(request):
                return self.render_then_finish(request)
            return self.call(
                "render_shader",
                {"glsl_code": glsl, "parameter_specs": [{"name": "uRed", "type": "float", "default": 0.2, "min": 0.0, "max": 1.0}]},
            )

        self.response = fixture
        started = start_workflow(package.source.reference_path, "让左右颜色与参考图一致", output_dir=self.root / "workflows", max_output_tokens=1000)
        self.assertEqual(started.phase, "gate_a", started.reason)
        root = Path(started.directory)
        result = advance_workflow(root, GateADecision(action="generate_scene", sketch_id="S1", alternative_index=0))
        self.assertEqual(result.phase, "gate_b", result.reason)
        self.assertEqual(len(reviews), 2)
        before, after = result.candidates
        self.assertIsNotNone(before.diagnosis_path)
        self.assertTrue(before.major_issues)
        self.assertEqual(result.current_candidate, after)
        self.assertEqual(after.verdict, "no_major_issue_observed")
        self.assertEqual(before.code_sha256, after.code_sha256)
        self.assertEqual(result.optimization["status"], "improved")
        self.assertTrue(result.attempts[-1].automatic)
        with Image.open(root / before.preview_path) as baseline, Image.open(root / after.preview_path) as tuned:
            self.assertEqual(baseline.crop((16, 0, 32, 24)).tobytes(), tuned.crop((16, 0, 32, 24)).tobytes())
            self.assertAlmostEqual(tuned.getpixel((8, 8))[0], 160, delta=2)
