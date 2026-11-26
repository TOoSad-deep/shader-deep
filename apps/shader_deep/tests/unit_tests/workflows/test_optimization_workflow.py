"""无模型网络条件下核对整体工作流门禁及制品身份."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.domain.blackboard import new_blackboard
from shader_deep.domain.review_items import CheckAnswer, CheckFinding
from shader_deep.domain.scene_planning import ObservationRevisionRequest, RiskProbe
from shader_deep.domain.shader_parameters import ParameterSpec
from shader_deep.domain.tasks import CandidateRecord, ResultRecord
from shader_deep.domain.visual_review import DimensionCriteria, PixelRegion, ReviewSample
from shader_deep.infrastructure.llm.budget import workflow_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.parameterized_candidate import CandidateParameters, save_baked_candidate
from shader_deep.infrastructure.storage.planning_package import read_planning_package, write_planning_package
from shader_deep.infrastructure.storage.review_package import read_review_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.infrastructure.storage.vista import read_history_json, read_image
from shader_deep.workflows.diagnosis import FindingDiagnosis, RevisionProposal
from shader_deep.workflows.numeric_revision import NumericRevision
from shader_deep.workflows.optimization_workflow import GateADecision, GateBDecision, advance_workflow, read_workflow, start_workflow
from shader_deep.workflows.optimization_workflow.candidates import _preferred
from shader_deep.workflows.optimization_workflow.storage import _save
from shader_deep.workflows.outcomes import AnalysisOutcome
from shader_deep.workflows.planning.contracts import PlanningOutcome
from shader_deep.workflows.planning.review_criteria import SceneCriteriaDraft, draft_scene_review_criteria
from tests.unit_tests.agents.test_native_analysis import NativeRoutingModel, call
from tests.unit_tests.fixtures._planning_fixture import planning_report
from tests.unit_tests.workflows.test_upstream_review_criteria import scene_task

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from shader_deep.agents.generation.options import GenerationOptions
    from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs
    from shader_deep.workflows.analysis.contracts import AnalysisRevision
    from shader_deep.workflows.optimization_workflow import WorkflowState
    from shader_deep.workflows.options import AnalysisOptions
    from shader_deep.workflows.planning.cascade import PlanningRevision
    from shader_deep.workflows.planning.options import PlanningOptions
    from shader_deep.workflows.review.session import ReviewSession

_REQUEST = "复现参考图"
_CODE = "uniform float u_gain;\nvoid mainImage(out vec4 c, in vec2 p) {c = vec4(vec3(u_gain), 1.0);}"


class OptimizationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture = planning_report(self.root / "fixture")
        source = read_planning_package(self.fixture).source
        self.reference = source.reference_path
        self.observation = source.observation_dir
        self.requested_revisions: list[AnalysisRevision | None] = []
        self.block_planning = False
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.stages.run_analysis", side_effect=self.observe))
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.stages.run_planning", side_effect=self.plan))
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria", return_value=scene_task()))
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate))
        self.enterContext(patch("shader_deep.workflows.review.entry._invoke", side_effect=self.accept_checks))

    def observe(
        self, _reference: Path, _request: str, *, options: AnalysisOptions, revision: AnalysisRevision | None, **_kwargs: object
    ) -> AnalysisOutcome:
        self.requested_revisions.append(revision)
        directory = create_run_directory(options.output_dir)
        package = shutil.copytree(self.observation, directory / "observations")
        write_snapshot(directory / "result.json", {"execution": {"model_calls": 1}})
        return AnalysisOutcome(
            state=new_blackboard(), task_id="A1", summary_result=None, run_dir=directory, stop_reason="completed", report_dir=package
        )

    def plan(self, _observation: Path, _request: str, *, options: PlanningOptions, **_kwargs: object) -> PlanningOutcome:
        directory = create_run_directory(options.output_dir)
        if self.block_planning:
            dispute = ObservationRevisionRequest(
                disputed_ids=("F8",),
                contradiction="局部锚点在观察中方向相反",
                suggested_region=PixelRegion(left=1, top=1, right=8, bottom=8),
            )
            return PlanningOutcome(
                run_dir=directory,
                report_dir=None,
                status="failed",
                error="blocking observation dispute",
                execution={"model_calls": 1},
                observation_revision_request=dispute,
            )
        package = shutil.copytree(self.fixture, directory / "planning")
        return PlanningOutcome(run_dir=directory, report_dir=package, status="completed", execution={"model_calls": 1})

    def generate(self, _plan: Path, *, options: GenerationOptions, **_kwargs: object) -> GenerationOutcome:
        directory = create_run_directory(options.output_dir)
        code, preview, record = directory / "shader.glsl", directory / "candidate.png", directory / "parameters.json"
        code.write_text(_CODE, encoding="utf-8")
        shutil.copyfile(self.reference, preview)
        spec = ParameterSpec(name="u_gain", type="float", min=0.0, max=1.0, default=0.5, target_ref="F8")
        CandidateParameters(specs=(spec,), uniforms={"u_gain": 0.75}).save(record, code, preview, time=options.time)
        candidate = CandidateRecord(id="C1", task_id="G1", code_path=str(code), preview_path=str(preview), render_record_path=str(record))
        return GenerationOutcome(
            state=new_blackboard(),
            run_dir=directory,
            selected_candidate=candidate,
            stop_reason="completed",
            attempts=1,
            model_calls=2,
            baked_code_path=save_baked_candidate(record, code, preview),
        )

    @staticmethod
    def accept_checks(session: ReviewSession, _usage: object) -> None:
        session.model_calls = 1
        session.finish(
            "固定单元评审",
            answers=tuple(
                CheckAnswer(check_id=item.check_id, rationale="固定测试中双图一致。") for item in session.checks if item.unassessed is None
            ),
        )

    def test_promoting_a_rerendered_local_probe_preserves_its_reviewed_frame(self) -> None:
        root = Path(self.generate_one(max_rounds=5).directory)
        local = GateBDecision(
            action="reroute",
            reason="复核局部色场",
            route="code_structure",
            target_region=PixelRegion(left=0, top=0, right=8, bottom=8),
            probe_override=RiskProbe(target_ids=("F8",), reason="检验渐变", context_mode="isolated_solid"),
        )
        probed = advance_workflow(root, local)
        self.assertEqual(probed.current_candidate.probe.time, 0.0)
        with patch("shader_deep.workflows.numeric_revision.WebGL2Renderer") as renderer:
            renderer.return_value.__enter__.return_value.render.return_value = self.reference.read_bytes()
            revised = advance_workflow(root, GateBDecision(action="reroute", reason="查看另一帧", route="evidence_gap", time=2.5))
        self.assertEqual(revised.current_candidate.probe.time, 2.5)
        self.assertEqual(revised.pending_probe.time, 2.5)
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate) as generate:
            state = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual(state.current_candidate.scope, "scene")
        self.assertEqual(state.current_candidate.time, 2.5)
        (snippet,) = generate.call_args.kwargs["verified_snippets"]
        self.assertEqual(snippet.binding.time, 2.5)

    def generate_one(self, *, max_rounds: int = 4) -> WorkflowState:
        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", max_rounds=max_rounds)
        self.assertEqual((start.phase, start.remaining_model_calls), ("gate_a", 97))
        root = Path(start.directory)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            advance_workflow(root, GateBDecision(action="accept"))
        ready = advance_workflow(root, GateADecision(action="generate_scene", sketch_id="S1", expected_revision=start.revision))
        self.assertEqual((ready.phase, ready.remaining_model_calls, ready.round), ("gate_b", 94, 1))
        self.assertEqual(ready, read_workflow(root))
        return ready

    def test_gate_a_rejects_either_out_of_bounds_sample_before_freezing_and_generation(self) -> None:
        ready = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        root = Path(ready.directory)
        valid = PixelRegion(left=0, top=0, right=ready.width, bottom=ready.height)
        valid_sample = ReviewSample(label="核对整图颜色", reference_region=valid, rendered_region=valid)
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate) as generate:
            for role, out_of_bounds in (
                ("reference", valid.model_copy(update={"right": ready.width + 1})),
                ("rendered", valid.model_copy(update={"bottom": ready.height + 1})),
            ):
                with self.subTest(role=role):
                    sample = valid_sample.model_copy(update={f"{role}_region": out_of_bounds})
                    criteria = scene_task().model_copy(update={"samples": (sample,)})
                    decision = GateADecision(action="generate_scene", sketch_id="S1", criteria=criteria, expected_revision=ready.revision)
                    with self.assertRaisesRegex(ValueError, f"核对整图颜色.*{role} region"):
                        advance_workflow(root, decision)
                    self.assertEqual(read_workflow(root), ready)
                    self.assertFalse(tuple(root.glob("criteria-frozen-*.json")))
                    generate.assert_not_called()
            criteria = scene_task().model_copy(update={"samples": (valid_sample,)})
            decision = GateADecision(action="generate_scene", sketch_id="S1", criteria=criteria, expected_revision=ready.revision)
            generated = advance_workflow(root, decision)
        self.assertEqual(generated.phase, "gate_b")
        generate.assert_called_once()
        report = read_review_package(root / generated.current_candidate.review_path)
        self.assertEqual(report.source.request.samples, (valid_sample,))

    def test_two_approved_probes_are_reviewed_and_promoted_before_scene_generation(self) -> None:
        package = read_planning_package(self.fixture)
        probes = tuple(RiskProbe(target_ids=(ref,), reason="局部形态需要验证", context_mode="isolated_solid") for ref in ("F8", "F15"))
        updated = package.report.sketches[0].model_copy(update={"risk_probes": probes})
        self.fixture = write_planning_package(self.root / "two-probes", package.source, package.report.model_copy(update={"sketches": (updated,)}))
        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        root = Path(start.directory)
        selections = [{"probe_id": index, "target_region": {"left": 0, "top": 0, "right": 8, "bottom": 8}} for index in range(2)]
        choice = GateADecision.model_validate({"action": "run_probe", "sketch_id": "S1", "probes": selections})
        first = advance_workflow(root, choice)
        self.assertEqual(first.current_candidate.probe.target_ids, ("F8",))
        self.assertEqual(read_workflow(root).queued_probes[0].target_ids, ("F15",))
        second = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual((second.verified_probes[0].candidate_id, second.current_candidate.probe.target_ids), (first.current_candidate.id, ("F15",)))
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate) as generate:
            scene = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual((scene.current_candidate.scope, scene.round), ("scene", 3))
        self.assertEqual([snippet.binding.target_ids for snippet in generate.call_args.kwargs["verified_snippets"]], [("F8",), ("F15",)])

    def compared_render(self, _state: object, task_id: str, _root: Path, _options: object, *, run_dir: Path, **_kwargs: object) -> GenerationOutcome:
        code, preview = run_dir / "candidate.glsl", run_dir / "candidate.png"
        code.write_text("void mainImage(out vec4 c, in vec2 p) { c=vec4(1.); }", encoding="utf-8")
        shutil.copyfile(self.reference, preview)
        candidate = CandidateRecord(id="C1", task_id=task_id, code_path=str(code), preview_path=str(preview))
        return GenerationOutcome(
            state=new_blackboard(), run_dir=run_dir, selected_candidate=candidate, stop_reason="completed", attempts=1, model_calls=2
        )

    def test_gate_can_choose_an_independently_reviewed_comparison_branch_without_regenerating(self) -> None:
        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        root = Path(start.directory)
        choice = GateADecision.model_validate(
            {"action": "compare_plans", "sketch_id": "S1", "comparison_plan": {"sketch_id": "S1", "alternative_index": 0}}
        )
        model = FakeListChatModel(responses=["共用的离线生成模型"])

        def compare(
            state: object, task: str, root: Path, options: object, *, run_dir: Path, inputs: CapturedPlanningInputs, model: BaseChatModel
        ) -> GenerationOutcome:
            del inputs
            self.assertEqual(model.invoke("对照生成请求").content, "共用的离线生成模型")
            return self.compared_render(state, task, root, options, run_dir=run_dir)

        with (
            patch("shader_deep.workflows.generation_comparison._run_session", side_effect=compare) as calls,
            patch("shader_deep.workflows.generation_comparison.build_model") as build_model,
        ):
            compared = advance_workflow(root, choice, model=model)
        build_model.assert_not_called()
        self.assertEqual(compared.request_attempts, 2)
        self.assertEqual((len(compared.comparison_candidate_ids), calls.call_count, compared.phase), (2, 2, "gate_a"))
        self.assertEqual(compared.remaining_model_calls, start.remaining_model_calls - 6)
        chosen = compared.comparison_candidate_ids[1]
        ready = advance_workflow(root, GateADecision(action="select_comparison", candidate_id=chosen))
        self.assertEqual((ready.phase, ready.current_candidate.id, ready.alternative_index, ready.round), ("gate_b", chosen, 0, 1))
        self.assertEqual(ready.remaining_model_calls, compared.remaining_model_calls)
        accepted = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual(accepted.phase, "completed")

    def test_gate_comparison_reuses_only_snippets_common_to_both_choices(self) -> None:
        package = read_planning_package(self.fixture)
        probes = tuple(RiskProbe(target_ids=(ref,), reason="局部方法需要验证", context_mode="isolated_solid") for ref in ("F8", "R9"))
        sketch = package.report.sketches[0].model_copy(update={"risk_probes": probes})
        self.fixture = write_planning_package(
            self.root / "probes-for-comparison", package.source, package.report.model_copy(update={"sketches": (sketch,)})
        )
        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", time=1.5, max_rounds=5)
        root = Path(start.directory)
        selections = [{"probe_id": index, "target_region": {"left": 0, "top": 0, "right": 8, "bottom": 8}} for index in range(2)]
        choice = GateADecision.model_validate({"action": "run_probe", "sketch_id": "S1", "probes": selections})
        advance_workflow(root, choice)
        advance_workflow(root, GateBDecision(action="accept"))
        scene = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual(len(scene.verified_probes), 2)
        gate = advance_workflow(root, GateBDecision(action="reroute", route="planning_limit", reason="比较替代方法的整图效果"))
        self.assertEqual(gate.phase, "gate_a")

        selection = GateADecision.model_validate(
            {"action": "compare_plans", "sketch_id": "S1", "comparison_plan": {"sketch_id": "S1", "alternative_index": 0}}
        )
        with (
            patch("shader_deep.workflows.generation_comparison._run_session", side_effect=self.compared_render) as calls,
            patch("shader_deep.workflows.generation_comparison.build_model", return_value=SimpleNamespace(model_name="offline")),
        ):
            compared = advance_workflow(root, selection)
        self.assertEqual(len(compared.comparison_candidate_ids), 2)
        self.assertEqual(calls.call_count, 2)
        for invocation in calls.call_args_list:
            (snippet,) = invocation.kwargs["inputs"].verified_snippets
            self.assertEqual(json.loads(snippet.metadata_json)["binding"]["target_ids"], ["F8"])

    def test_token_exhaustion_archives_the_reviewed_sibling_from_a_parallel_comparison(self) -> None:
        def render(
            state: object, task: str, root: Path, options: object, *, run_dir: Path, inputs: CapturedPlanningInputs, **_kwargs: object
        ) -> GenerationOutcome:
            if inputs.binding.alternative is not None:
                model = workflow_model(FakeListChatModel(responses=["只返回一次"]))
                model.invoke("预留并用尽剩余输出")
                model.invoke("此请求必须被账本拦截")
            return self.compared_render(state, task, root, options, run_dir=run_dir)

        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", max_output_tokens=1)
        root = Path(start.directory)
        decision = GateADecision.model_validate(
            {"action": "compare_plans", "sketch_id": "S1", "comparison_plan": {"sketch_id": "S1", "alternative_index": 0}}
        )
        with (
            patch("shader_deep.workflows.generation_comparison._run_session", side_effect=render),
            patch("shader_deep.workflows.generation_comparison.build_model", return_value=SimpleNamespace(model_name="offline")),
        ):
            advance_workflow(root, decision)
        archived = read_workflow(root)
        self.assertEqual((archived.phase, archived.remaining_output_tokens, archived.request_attempts), ("blocked", 0, 1))
        self.assertEqual(archived.comparison_candidate_ids, (archived.best_candidate.id,))
        record = json.loads((root / archived.comparison_path).read_bytes())
        self.assertIn("ModelBudgetExhaustedError", record["items"][1]["error"])
        best = archived.best_candidate
        self.assertEqual(read_review_package(root / best.review_path).content_sha256, best.review_sha256)
        self.assertEqual(read_history_json(root, archived.vista_history_record)["best_candidate"], best.id)

    def test_explicit_scene_stop_archives_its_reason_while_a_render_limit_returns_to_gate_b(self) -> None:
        for reason, phase in (("blocked", "blocked"), ("attempt_limit", "gate_b")):
            with self.subTest(reason=reason):
                start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
                directory = create_run_directory(Path(start.directory) / "generation")
                board = new_blackboard()
                if reason == "blocked":
                    record = ResultRecord(
                        id=f"{directory.name}-blocked", task_id="G1", status="blocked", summary="缺少必需的背景", recommendation="provide_input"
                    )
                    board["results"][record.id] = record
                outcome = GenerationOutcome(state=board, run_dir=directory, selected_candidate=None, stop_reason=reason, attempts=1, model_calls=1)
                with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", return_value=outcome):
                    state = advance_workflow(Path(start.directory), GateADecision(action="generate_scene", sketch_id="S1"))
                self.assertEqual(state.phase, phase)
                self.assertIn(reason, state.reason)
                if reason == "blocked":
                    self.assertIn("缺少必需的背景", state.reason)
                    self.assertIn("provide_input", state.reason)

    def test_retry_criteria_reuses_only_valid_unfrozen_packages_and_remaining_budget(self) -> None:
        request = read_planning_package(self.fixture).source.request
        with self.assertRaises(ValidationError) as rejected:
            SceneCriteriaDraft.model_validate({})
        retry = GateADecision(action="retry_criteria")
        for budget in (3, 10):
            with self.subTest(budget=budget):
                with patch("shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria", side_effect=rejected.exception):
                    state = start_workflow(self.reference, request, output_dir=self.root / "runs", max_model_calls=budget)
                root = Path(state.directory)
                prior_trace = (root / "workflow-trace.jsonl").read_bytes()
                self.assertEqual(state.phase, "blocked")
                if not state.remaining_model_calls:
                    with self.assertRaisesRegex(ValueError, "remaining budget"):
                        advance_workflow(root, retry)
                    self.assertEqual(read_workflow(root), state)
                    continue
                draft = scene_task().model_copy(update={"requirements": request})
                with patch("shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria", return_value=draft) as organizer:
                    ready = advance_workflow(root, retry)
                organizer.assert_called_once()
                self.assertEqual((ready.phase, ready.reason, ready.remaining_model_calls), ("gate_a", None, budget - 4))
                self.assertEqual(
                    (ready.observation_versions, ready.planning_path, ready.round), (state.observation_versions, state.planning_path, state.round)
                )
                self.assertTrue((root / "workflow-trace.jsonl").read_bytes().startswith(prior_trace))
                archived = advance_workflow(root, GateADecision(action="archive", reason=state.reason))
                with self.assertRaisesRegex(ValueError, "unfrozen first criteria"):
                    advance_workflow(root, retry)
                self.assertEqual(archived, read_workflow(root))

    def test_gate_a_redraft_keeps_the_published_version_until_the_reasoned_replacement_succeeds(self) -> None:
        request = read_planning_package(self.fixture).source.request
        draft = scene_task().model_copy(update={"requirements": request})
        generator = "shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria"
        with patch(generator, return_value=draft):
            state = start_workflow(self.reference, request, output_dir=self.root / "runs", max_model_calls=10)
        root = Path(state.directory)
        original = (root / state.criteria_path).read_bytes()
        with self.assertRaisesRegex(ValueError, "explicit reason"):
            advance_workflow(root, GateADecision(action="retry_criteria"))
        reason = "每项检查只有一个字, 无法据此验收"
        choice = GateADecision(action="retry_criteria", reason=reason)
        with patch(generator, side_effect=ValueError("无效标准")):
            failed = advance_workflow(root, choice)
        self.assertEqual((failed.phase, failed.criteria_path), ("gate_a", state.criteria_path))
        self.assertIn("无效标准", failed.reason)
        replacement = draft.model_copy(update={"target": "依照实测区域检查整图"})
        with patch(generator, return_value=replacement) as organizer:
            ready = advance_workflow(root, choice)
        self.assertEqual(organizer.call_args.kwargs["redraft_reason"], reason)
        self.assertEqual((ready.phase, ready.reason, ready.round, ready.remaining_model_calls), ("gate_a", None, 0, state.remaining_model_calls - 2))
        self.assertEqual(state.observation_versions, ready.observation_versions)
        self.assertEqual(state.planning_path, ready.planning_path)
        self.assertIsNone(ready.frozen_criteria_path)
        self.assertNotEqual(state.criteria_path, ready.criteria_path)
        self.assertEqual((root / state.criteria_path).read_bytes(), original)
        self.assertEqual(json.loads((root / ready.criteria_path).read_bytes()), replacement.model_dump(mode="json"))
        trace = [json.loads(line) for line in (root / "workflow-trace.jsonl").read_text().splitlines()]
        self.assertEqual(
            [row["stage"] for row in trace if row["event"] == "stage_start"], ["analysis", "planning", "criteria", "criteria", "criteria"]
        )
        self.assertEqual(
            (trace[-1]["event"], trace[-1]["previous_draft"], trace[-1]["new_draft"], trace[-1]["reason"]),
            ("criteria_redrafted", state.criteria_path, ready.criteria_path, reason),
        )

    def test_criteria_retry_recovers_invalid_model_sources_and_regions(self) -> None:
        request = read_planning_package(self.fixture).source.request
        valid = scene_task().model_copy(update={"requirements": request})
        invalid_source = {"dimensions": scene_task(source="F99").model_dump(mode="json")["dimensions"]}
        valid_output = {"dimensions": valid.model_dump(mode="json")["dimensions"]}
        invalid_region = {
            **valid_output,
            "required_regions": [
                {
                    "dimension": "color",
                    "check": "保留后圆的渐变",
                    "region": {"left": 0, "top": 0, "right": 33, "bottom": 8},
                }
            ],
        }
        organizer = "shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria"
        for invalid, legacy in ((invalid_source, False), (invalid_region, False), (invalid_source, True), (invalid_region, True)):
            with self.subTest(invalid=invalid, legacy=legacy), patch(organizer, side_effect=draft_scene_review_criteria):
                model = NativeRoutingModel(responses=[call("SceneCriteriaDraft", invalid), call("SceneCriteriaDraft", valid_output)])
                blocked = start_workflow(self.reference, request, output_dir=self.root / "runs", max_model_calls=10, model=model)
                self.assertEqual((blocked.phase, blocked.criteria_path), ("blocked", None))
                root = Path(blocked.directory)
                trace_path = root / "workflow-trace.jsonl"
                trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
                self.assertTrue(trace[-2]["invalid_output"])
                if legacy:
                    trace[-2]["invalid_output"] = False
                    trace_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trace))
                ready = advance_workflow(root, GateADecision(action="retry_criteria"), model=model)
                self.assertEqual(
                    (ready.phase, ready.planning_path, ready.observation_path), ("gate_a", blocked.planning_path, blocked.observation_path)
                )
                self.assertEqual(ready.remaining_model_calls, blocked.remaining_model_calls - 1)
                self.assertEqual(json.loads((root / ready.criteria_path).read_bytes()), valid.model_dump(mode="json"))
                self.assertEqual(len(model.requests), 2)

    def test_criteria_retry_does_not_recover_unrelated_value_errors(self) -> None:
        organizer = "shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria"
        with patch(organizer, side_effect=ValueError("固定参考图身份变化")):
            blocked = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", max_model_calls=10)
        self.assertEqual(blocked.phase, "blocked")
        root = Path(blocked.directory)
        with self.assertRaisesRegex(ValueError, "invalid model output"):
            advance_workflow(root, GateADecision(action="retry_criteria"))
        self.assertEqual(read_workflow(root), blocked)

    def test_only_human_acceptance_publishes_the_parameterized_and_baked_shader(self) -> None:
        ready = self.generate_one()
        root = Path(ready.directory)
        self.assertIsNone(ready.delivery_path)
        self.assertEqual(ready.current_candidate, ready.best_candidate)
        with self.assertRaisesRegex(ValueError, "state changed"):
            advance_workflow(root, GateBDecision(action="accept", expected_revision=0))
        accepted = advance_workflow(root, GateBDecision(action="accept", expected_revision=ready.revision))
        self.assertEqual(accepted.phase, "completed")
        self.assertEqual(accepted.delivery_path, "delivery")
        delivery = root / "delivery"
        self.assertIn("uniform float u_gain", (delivery / "shader.glsl").read_text(encoding="utf-8"))
        self.assertIn("const float u_gain = 0.75", (delivery / "shader.baked.glsl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((delivery / "uniforms.json").read_bytes()), {"u_gain": 0.75})
        history = read_history_json(root, accepted.vista_history_record)
        self.assertEqual(history["best_candidate"], accepted.best_candidate.id)
        self.assertEqual(read_history_json(root, history["previous"])["phase"], "gate_b")
        image, shader, render = read_image(root / "vista", accepted.best_candidate.vista_record)
        self.assertEqual((image, shader), ((delivery / "preview.png").read_bytes(), (delivery / "shader.glsl").read_bytes()))
        self.assertEqual(render, {"time": 0.0, "uniforms": {"u_gain": 0.75}})
        review = read_history_json(root, accepted.best_candidate.vista_review_record)
        self.assertEqual(review["verdict"], "no_major_issue_observed")

    def test_tampered_parameter_record_cannot_be_accepted(self) -> None:
        ready = self.generate_one()
        root = Path(ready.directory)
        candidate = ready.current_candidate
        if candidate is None or candidate.render_record_path is None:
            self.fail("expected a candidate with parameters")
        parameter_file = root / candidate.render_record_path
        payload = json.loads(parameter_file.read_bytes())
        payload["render"]["uniforms"]["u_gain"] = 0.5
        write_snapshot(parameter_file, payload)
        with self.assertRaisesRegex(ValueError, "candidate sidecar has changed"):
            advance_workflow(root, GateBDecision(action="accept"))
        self.assertIsNone(read_workflow(root).delivery_path)

    def test_planning_automatically_rechecks_an_observation_only_once(self) -> None:
        self.block_planning = True
        state = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        self.assertEqual((state.phase, state.observation_rechecks), ("gate_a", 1))
        self.assertIsNotNone(state.observation_revision_request)
        self.assertIsNone(state.planning_path)
        self.assertEqual(len(self.requested_revisions), 2)
        self.assertIsNone(self.requested_revisions[0])
        self.assertIsNotNone(self.requested_revisions[1])
        self.assertIsNone(state.criteria_path)
        self.assertEqual(state.remaining_model_calls, 96)

    def test_gate_a_replan_can_change_a_plan_outside_the_automatic_observation_scope(self) -> None:
        initial = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        root = Path(initial.directory)
        plan = read_planning_package(root / initial.planning_path)
        changed_method = "按人工要求重新组合相交区域。"
        mechanisms = tuple(row.model_copy(update={"method": changed_method}) if row.id == "M3" else row for row in plan.report.mechanisms)
        changed = plan.report.model_copy(update={"mechanisms": mechanisms})

        def replan(
            observation: Path,
            request: str,
            *,
            options: PlanningOptions,
            revision: PlanningRevision | None,
            revision_feedback: tuple[str, ...],
            **_kwargs: object,
        ) -> PlanningOutcome:
            if observation == root / initial.observation_path:
                self.block_planning = True
                return self.plan(observation, request, options=options)
            self.assertIn("调整 M3 关系组合", revision_feedback)
            if revision is not None:
                revision.validate(changed)
            run = create_run_directory(options.output_dir)
            published = write_planning_package(run / "planning", plan.source, changed)
            return PlanningOutcome(run_dir=run, report_dir=published, status="completed", execution={"model_calls": 1})

        with patch("shader_deep.workflows.optimization_workflow.stages.run_planning", side_effect=replan):
            ready = advance_workflow(root, GateADecision(action="replan", reason="调整 M3 关系组合"))
        self.assertEqual((ready.phase, ready.observation_rechecks), ("gate_a", 1))
        self.assertTrue(ready.observation_cascades)
        self.assertEqual((root / ready.criteria_path).read_bytes(), (root / initial.criteria_path).read_bytes())
        published = read_planning_package(root / ready.planning_path)
        self.assertEqual(next(row.method for row in published.report.mechanisms if row.id == "M3"), changed_method)

    def test_ordinary_planning_stop_preserves_reason_even_with_no_budget_left(self) -> None:
        failure = PlanningOutcome(run_dir=self.root, report_dir=None, status="failed", error="无法发布规划", execution={"model_calls": 1})
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.stages.run_planning", return_value=failure))
        draft = self.enterContext(patch("shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria"))
        state = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", max_model_calls=2)
        self.assertEqual((state.phase, state.reason, state.remaining_model_calls), ("blocked", failure.error, 0))
        self.assertIsNone(state.planning_path)
        self.assertIsNone(state.criteria_path)
        draft.assert_not_called()

    def test_accepting_bottleneck_recommendation_changes_structure_after_reload(self) -> None:
        draft = scene_task()
        dimensions = tuple(
            DimensionCriteria(id=item.id, required_checks=("检查后圆",), source_refs=("F8",))
            if item.id == "color"
            else DimensionCriteria(id=item.id, excluded_checks=("本次仅核对后圆颜色",))
            for item in draft.dimensions
        )
        self.enterContext(
            patch(
                "shader_deep.workflows.optimization_workflow.stages.draft_scene_review_criteria",
                return_value=draft.model_copy(update={"dimensions": dimensions}),
            )
        )

        def mismatch(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            finding = CheckFinding(region="圆内", reference_observation="暗", rendered_observation="亮", severity="major", impact="颜色方向相反")
            answer = CheckAnswer(check_id=session.checks[0].check_id, rationale="同一区域有可见差异", findings=(finding,))
            session.finish("候选需要修订", answers=(answer,))

        def diagnose(report_dir: Path, _plan_dir: Path, **_kwargs: object) -> RevisionProposal:
            package = read_review_package(report_dir)
            issue = package.report.action_summary.major_issues[0]
            item = FindingDiagnosis(
                issue_id=issue.id, source_ref="F8", route="numeric_tuning", target="核对后圆亮度", evidence=("区域对照",), parameter_names=("u_gain",)
            )
            return RevisionProposal(items=(item,), review_sha256=package.content_sha256, model_calls=1)

        self.enterContext(patch("shader_deep.workflows.review.entry._invoke", side_effect=mismatch))
        self.enterContext(patch("shader_deep.workflows.optimization_workflow.candidates.run_diagnosis", side_effect=diagnose))
        failed = NumericRevision(run_dir=self.root, candidate=None, baked_code_path=None, diagnostics={"status": "structure_bottleneck"})
        numeric = self.enterContext(patch("shader_deep.workflows.optimization_workflow.revisions.run_numeric_revision", return_value=failed))
        started = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs")
        root = Path(started.directory)
        advance_workflow(root, GateADecision(action="generate_scene", sketch_id="S1"))
        stalled = read_workflow(root)
        self.assertTrue(any("numeric_tuning → code_structure" in item for item in stalled.escalations))
        generated = self.enterContext(
            patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate)
        )
        revised = advance_workflow(root, GateBDecision(action="revise", expected_revision=stalled.revision))
        self.assertEqual(generated.call_args.kwargs["revision_candidate"].id, stalled.current_candidate.native_id)
        self.assertIn("后圆亮度", generated.call_args.kwargs["revision_guidance"])
        self.assertEqual(generated.call_count, 1)
        second = advance_workflow(root, GateBDecision(action="revise", expected_revision=revised.revision))
        self.assertEqual(generated.call_count, 2)
        self.assertEqual(numeric.call_count, 3)
        self.assertTrue(any("code_structure → planning_limit" in item for item in second.escalations))
        replanned = advance_workflow(root, GateBDecision(action="revise", expected_revision=second.revision))
        self.assertEqual(replanned.phase, "gate_a")
        self.assertEqual(generated.call_count, 2)

    def test_round_budget_archives_the_best_reviewed_candidate(self) -> None:
        ready = self.generate_one(max_rounds=1)
        blocked = advance_workflow(Path(ready.directory), GateBDecision(action="reroute", route="code_structure", reason="重新检查结构"))
        self.assertEqual(blocked.phase, "blocked")
        self.assertEqual(blocked.best_candidate, ready.best_candidate)
        self.assertIn("round budget", blocked.reason or "")

    def test_rollback_fork_counts_only_its_own_failed_revisions(self) -> None:
        def mismatch(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            finding = CheckFinding(region="后圆", reference_observation="暗", rendered_observation="亮", severity="major", impact="色差")
            session.finish(
                "后圆待修订",
                answers=tuple(
                    CheckAnswer(check_id=item.check_id, rationale="核对后圆亮度", findings=(finding,) if item.source_ref == "F8" else ())
                    for item in session.checks
                ),
            )

        self.enterContext(patch("shader_deep.workflows.review.entry._invoke", side_effect=mismatch))
        self.enterContext(
            patch("shader_deep.workflows.optimization_workflow.entry._diagnose", side_effect=lambda _context, candidate: (candidate, None))
        )
        initial = self.generate_one(max_rounds=5)
        root = Path(initial.directory)
        revise = GateBDecision(action="reroute", route="code_structure", reason="重新检查结构")
        abandoned = advance_workflow(root, revise)
        self.assertEqual(abandoned.escalation_routes(), {})
        restored = advance_workflow(root, GateBDecision(action="rollback", candidate_id=initial.current_candidate.id))
        self.assertEqual(restored.escalation_routes(), {})
        fork = advance_workflow(root, revise)
        self.assertEqual(read_workflow(root).escalation_routes(), {})
        self.assertNotEqual(fork.current_candidate.id, abandoned.current_candidate.id)
        continued = advance_workflow(root, revise)
        self.assertEqual(set(continued.escalation_routes().values()), {("code_structure", "planning_limit")})
        self.assertEqual(read_workflow(root).escalation_routes(), continued.escalation_routes())

    def test_best_candidate_prefers_complete_review_and_compares_only_paired_errors(self) -> None:
        reviewed = self.generate_one().current_candidate
        incomplete = reviewed.model_copy(
            update={"id": "incomplete", "verdict": "inconclusive", "remaining_issues": ("F8:color",), "objective_error": 0.1}
        )
        self.assertEqual(_preferred((incomplete, reviewed), None), reviewed)
        self.assertEqual(_preferred((incomplete, reviewed), incomplete.id), incomplete)

        start = reviewed.model_copy(update={"objective_run": "revisions/paired", "objective_error": 0.4})
        end = start.model_copy(update={"id": "optimized", "objective_error": 0.1})
        self.assertEqual(_preferred((start, end), None), end)
        for update in ({"objective_run": "revisions/other"}, {"time": 3.0}, {"objective_run": None}):
            with self.subTest(update=update):
                self.assertEqual(_preferred((start, end.model_copy(update=update)), None), start)

    def test_default_rollback_rechecks_the_best_candidate_from_an_older_snapshot(self) -> None:
        def incomplete_review(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            session.finish(
                "静态帧缺证",
                answers=tuple(
                    CheckAnswer(check_id=item.check_id, rationale="当前帧复核", unassessed_reason="需要另一帧" if item.source_ref == "F8" else None)
                    for item in session.checks
                ),
            )

        with (
            patch("shader_deep.workflows.review.entry._invoke", side_effect=incomplete_review),
            patch("shader_deep.workflows.optimization_workflow.entry._diagnose", side_effect=lambda _context, candidate: (candidate, None)),
        ):
            initial = self.generate_one()
        root = Path(initial.directory)
        first = initial.current_candidate.model_copy(update={"objective_error": 0.1})
        self.assertEqual(first.verdict, "inconclusive")
        with patch("shader_deep.workflows.numeric_revision.WebGL2Renderer") as renderer:
            renderer.return_value.__enter__.return_value.render.return_value = self.reference.read_bytes()
            reviewed = advance_workflow(root, GateBDecision(action="reroute", route="evidence_gap", reason="补查第二帧", time=2.0))
        second = reviewed.current_candidate
        self.assertEqual(second.verdict, "no_major_issue_observed")
        self.assertEqual(reviewed.best_candidate, second)
        _save(reviewed.model_copy(update={"candidates": (first, second), "best_candidate": first}))
        restored = advance_workflow(root, GateBDecision(action="rollback"))
        self.assertEqual(restored.current_candidate, second)

    def test_rollback_is_the_regression_baseline_until_the_scene_standard_changes(self) -> None:
        initial = self.generate_one(max_rounds=5)
        root = Path(initial.directory)
        revise = GateBDecision(action="reroute", route="code_structure", reason="重新检查结构")

        def mismatch(session: ReviewSession, _usage: object) -> None:
            session.model_calls = 1
            finding = CheckFinding(region="后圆", reference_observation="暗", rendered_observation="亮", severity="major", impact="色差")
            session.finish(
                "后圆待修订",
                answers=tuple(
                    CheckAnswer(check_id=item.check_id, rationale="核对后圆亮度", findings=(finding,) if item.source_ref == "F8" else ())
                    for item in session.checks
                ),
            )

        with (
            patch("shader_deep.workflows.review.entry._invoke", side_effect=mismatch),
            patch("shader_deep.workflows.optimization_workflow.entry._diagnose", side_effect=lambda _context, candidate: (candidate, None)),
        ):
            alternate = advance_workflow(root, revise)
            self.assertEqual(alternate.best_candidate.id, initial.current_candidate.id)
            self.assertTrue(alternate.current_candidate.regressions)
            selected = alternate.current_candidate.id
            advance_workflow(root, GateBDecision(action="rollback", candidate_id=selected))
            revised = advance_workflow(root, revise)
            self.assertEqual((revised.best_candidate.id, revised.manual_best_candidate_id), (selected, selected))
            self.assertEqual(revised.current_candidate.regressions, ())

        dispute = GateBDecision(action="reroute", route="observation_error", reason="参考图中后圆方向待核实", disputed_ids=("F8",))
        reopened = advance_workflow(root, dispute)
        self.assertEqual(reopened.phase, "gate_a")
        new_standard = scene_task(source="E2")
        regenerated = advance_workflow(root, GateADecision(action="generate_scene", sketch_id="S1", criteria=new_standard))
        self.assertNotEqual(regenerated.frozen_criteria_sha256, initial.frozen_criteria_sha256)
        self.assertIsNone(regenerated.manual_best_candidate_id)
        self.assertEqual(regenerated.best_candidate, regenerated.current_candidate)
        self.assertEqual(regenerated.current_candidate.regressions, ())

    def test_manual_local_rollback_clears_when_the_patch_is_promoted_to_scene(self) -> None:
        initial = self.generate_one(max_rounds=4)
        root = Path(initial.directory)
        choice = GateBDecision(
            action="reroute",
            reason="修补后圆颜色",
            route="code_structure",
            target_region=PixelRegion(left=0, top=0, right=8, bottom=8),
            probe_override=RiskProbe(target_ids=("F8",), reason="核对局部色场", context_mode="scene_baseline_patch"),
            patch_source="c = vec4(vec3(u_gain), 1.0);",
        )
        local = advance_workflow(root, choice).current_candidate
        first = advance_workflow(root, GateBDecision(action="accept")).current_candidate
        rolled_back = advance_workflow(root, GateBDecision(action="rollback", candidate_id=local.id))
        self.assertEqual(rolled_back.manual_best_candidate_id, local.id)
        repeated = advance_workflow(root, GateBDecision(action="accept"))
        self.assertIsNone(repeated.manual_best_candidate_id)
        self.assertNotEqual(repeated.current_candidate.id, first.id)
        self.assertIn(first, repeated.candidates)
        self.assertIsNotNone(repeated.current_candidate.review_path)
        self.assertEqual(repeated.best_candidate.scope, "scene")

    def test_rejects_an_unbalanced_patch_before_starting_generation(self) -> None:
        initial = self.generate_one()
        root = Path(initial.directory)
        choice = GateBDecision(
            action="reroute",
            reason="修补后圆颜色",
            route="code_structure",
            target_region=PixelRegion(left=0, top=0, right=8, bottom=8),
            probe_override=RiskProbe(target_ids=("F8",), reason="核对局部色场", context_mode="scene_baseline_patch"),
            patch_source="vec4(vec3(u_gain), 1.0",
        )
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning") as generate:
            with self.assertRaisesRegex(ValueError, "边界完整"):
                advance_workflow(root, choice)
            generate.assert_not_called()
        self.assertEqual(read_workflow(root).round, initial.round)

    def test_queued_probes_share_the_first_approved_frame(self) -> None:
        package = read_planning_package(self.fixture)
        probes = tuple(RiskProbe(target_ids=(ref,), reason="局部形态需要验证", context_mode="isolated_solid") for ref in ("F8", "F15"))
        updated = package.report.sketches[0].model_copy(update={"risk_probes": probes})
        self.fixture = write_planning_package(self.root / "two-probes", package.source, package.report.model_copy(update={"sketches": (updated,)}))
        start = start_workflow(self.reference, _REQUEST, output_dir=self.root / "runs", max_rounds=5)
        root = Path(start.directory)
        selections = [{"probe_id": index, "target_region": {"left": 0, "top": 0, "right": 8, "bottom": 8}} for index in range(2)]
        choice = GateADecision.model_validate({"action": "run_probe", "sketch_id": "S1", "probes": selections})
        advance_workflow(root, choice)
        rerender = GateBDecision(action="reroute", reason="查看另一帧", route="evidence_gap", time=2.5)
        with patch("shader_deep.workflows.numeric_revision.WebGL2Renderer") as renderer:
            renderer.return_value.__enter__.return_value.render.return_value = self.reference.read_bytes()
            advance_workflow(root, rerender)
        failed_generation = GenerationOutcome(
            state=new_blackboard(),
            run_dir=create_run_directory(root / "generation"),
            selected_candidate=None,
            stop_reason="model_limit",
            attempts=0,
            model_calls=1,
        )
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", return_value=failed_generation):
            failed = advance_workflow(root, GateBDecision(action="accept"))
        self.assertIsNone(failed.current_candidate)
        self.assertEqual(failed.pending_probe.time, 2.5)
        retry = GateBDecision(
            action="reroute",
            route="code_structure",
            reason="重新绑定第二项试验",
            probe_id=1,
            target_region=PixelRegion(left=0, top=0, right=8, bottom=8),
        )
        second = advance_workflow(root, retry)
        self.assertEqual(second.current_candidate.time, 2.5)
        with self.assertRaisesRegex(ValueError, "须使用同一帧"):
            advance_workflow(root, rerender.model_copy(update={"time": 3.0}))
        self.assertEqual(read_workflow(root), second)
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate) as generate:
            scene = advance_workflow(root, GateBDecision(action="accept"))
        self.assertEqual(scene.current_candidate.time, 2.5)
        snippets = generate.call_args.kwargs["verified_snippets"]
        self.assertEqual([(snippet.binding.target_ids, snippet.binding.time) for snippet in snippets], [(("F8",), 2.5), (("F15",), 2.5)])

    def test_unrelated_scene_exclusion_does_not_invalidate_a_verified_snippet(self) -> None:
        initial = self.generate_one(max_rounds=5)
        root = Path(initial.directory)
        local = GateBDecision(
            action="reroute",
            reason="复核局部色场",
            route="code_structure",
            target_region=PixelRegion(left=0, top=0, right=8, bottom=8),
            probe_override=RiskProbe(target_ids=("F8",), reason="检验渐变", context_mode="isolated_solid"),
        )
        advance_workflow(root, local)
        advance_workflow(root, GateBDecision(action="accept"))
        reopened = advance_workflow(root, GateBDecision(action="reroute", route="planning_limit", reason="无需单独检查交叠"))
        self.assertEqual(reopened.phase, "gate_a")
        prior = scene_task()
        dimensions = tuple(
            DimensionCriteria(id="relations", excluded_checks=("无需单独检查交叠",)) if row.id == "relations" else row for row in prior.dimensions
        )
        choice = GateADecision(action="generate_scene", sketch_id="S1", criteria=prior.model_copy(update={"dimensions": dimensions}))
        with patch("shader_deep.workflows.optimization_workflow.entry.run_generation_from_planning", side_effect=self.generate) as generate:
            advance_workflow(root, choice)
        (snippet,) = generate.call_args.kwargs["verified_snippets"]
        self.assertEqual(snippet.binding.target_ids, ("F8",))
