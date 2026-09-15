"""验证任务绑定、候选来源、结果交接与黑板更新行为."""

from dataclasses import replace
from unittest import TestCase

from shader_deep.blackboard import add_candidate, add_result, add_target, add_task, new_blackboard, read_task
from shader_deep.schemas import BlackboardState, CandidateRecord, ResultRecord, TargetRecord, TaskRecord


def _initial_state() -> BlackboardState:
    state = add_target(new_blackboard(), TargetRecord(version="T1", request="复刻倒影", reference_path="inputs/reference.png"))
    state = add_task(state, TaskRecord(id="G0", role="generation", target_version="T1", objective="实现粗稿"))
    state = add_candidate(state, CandidateRecord(id="B7", task_id="G0", code_path="candidates/B7/shader.glsl"))
    return add_task(state, TaskRecord(id="G1", role="generation", target_version="T1", baseline_id="B7", objective="修正倒影宽度"))


class BlackboardTests(TestCase):
    def setUp(self) -> None:
        self.state = _initial_state()

    def test_first_analysis_can_start_without_baseline(self) -> None:
        state = add_task(self.state, TaskRecord(id="A1", role="analysis", target_version="T1", objective="观察参考图"))
        records = read_task(state, "A1")
        self.assertEqual(records.target.reference_path, "inputs/reference.png")
        self.assertIsNone(records.baseline)
        self.assertEqual(records.inputs, ())
        self.assertEqual(records.outputs, ())

    def test_new_target_and_candidate_do_not_rebind_existing_task(self) -> None:
        state = add_target(self.state, TargetRecord(version="T2", request="调整整体色调", reference_path="inputs/reference.png"))
        state = add_candidate(state, CandidateRecord(id="B8", task_id="G0", code_path="candidates/B8/shader.glsl"))
        records = read_task(state, "G1")
        self.assertEqual(records.target.version, "T1")
        self.assertEqual(records.baseline, self.state["candidates"]["B7"])

    def test_updates_preserve_input_state_and_previous_read(self) -> None:
        previous = read_task(self.state, "G1")
        candidate = CandidateRecord(id="C12", task_id="G1", code_path="candidates/C12/shader.glsl")
        state = add_candidate(self.state, candidate)
        self.assertEqual(read_task(state, "G1").outputs, (candidate,))
        self.assertEqual(previous.outputs, ())
        self.assertEqual(read_task(self.state, "G1").outputs, ())
        self.assertNotIn("C12", self.state["candidates"])

    def test_duplicate_identifiers_cannot_replace_history(self) -> None:
        result = ResultRecord(id="R1", task_id="G1", status="partial", summary="第一次尝试")
        state = add_result(self.state, result)
        writes = (
            lambda: add_target(state, replace(state["targets"]["T1"], request="覆盖旧目标")),
            lambda: add_task(state, replace(state["tasks"]["G1"], baseline_id=None)),
            lambda: add_candidate(state, replace(state["candidates"]["B7"], code_path="different.glsl")),
            lambda: add_result(state, replace(result, summary="覆盖旧结果")),
        )
        for write in writes:
            with self.subTest(write=write), self.assertRaisesRegex(ValueError, "already exists"):
                write()
        self.assertEqual(read_task(state, "G1").baseline, self.state["candidates"]["B7"])
        self.assertEqual(read_task(state, "G1").results, (result,))

    def test_missing_task_inputs_are_rejected_before_registration(self) -> None:
        task = replace(self.state["tasks"]["G1"], id="invalid")
        invalid_tasks = (
            replace(task, target_version="missing-target"),
            replace(task, baseline_id="missing-baseline"),
            replace(task, candidate_ids=("missing-candidate",)),
            replace(task, related_result_ids=("missing-result",)),
        )
        for invalid in invalid_tasks:
            with self.subTest(task=invalid), self.assertRaisesRegex(ValueError, "Unknown"):
                add_task(self.state, invalid)
        self.assertNotIn("invalid", self.state["tasks"])

    def test_review_requires_an_explicit_candidate(self) -> None:
        task = TaskRecord(id="review", role="review", target_version="T1", objective="评审候选", baseline_id="B7")
        with self.assertRaisesRegex(ValueError, "requires at least one candidate"):
            add_task(self.state, task)

    def test_only_generation_tasks_can_register_candidates(self) -> None:
        tasks = (
            TaskRecord(id="A1", role="analysis", target_version="T1", objective="分析"),
            TaskRecord(id="V1", role="review", target_version="T1", objective="评审", candidate_ids=("B7",)),
        )
        for task in tasks:
            state = add_task(self.state, task)
            with self.subTest(role=task.role), self.assertRaisesRegex(ValueError, "Only generation"):
                add_candidate(state, CandidateRecord(id="invalid", task_id=task.id, code_path="invalid.glsl"))
            self.assertNotIn("invalid", state["candidates"])

    def test_candidate_requires_an_existing_source_task(self) -> None:
        with self.assertRaises(KeyError):
            add_candidate(self.state, CandidateRecord(id="invalid", task_id="missing", code_path="invalid.glsl"))
        self.assertNotIn("invalid", self.state["candidates"])

    def test_read_task_excludes_unrelated_candidates_and_results(self) -> None:
        candidate = CandidateRecord(id="C12", task_id="G1", code_path="candidates/C12/shader.glsl")
        state = add_candidate(self.state, candidate)
        state = add_result(state, ResultRecord(id="generation-result", task_id="G1", status="completed", summary="生成者自我评价"))
        task = TaskRecord(id="V1", role="review", target_version="T1", baseline_id="B7", candidate_ids=("C12",), objective="比较倒影")
        state = add_task(state, task)
        records = read_task(state, "V1")
        self.assertEqual(records.inputs, (candidate,))
        self.assertEqual(records.outputs, ())
        self.assertEqual(records.related_results, ())
        self.assertEqual(records.results, ())

    def test_result_can_reference_baseline_and_own_output_without_adoption(self) -> None:
        state = add_candidate(self.state, CandidateRecord(id="C12", task_id="G1", code_path="candidates/C12/shader.glsl"))
        result = ResultRecord(
            id="R1",
            task_id="G1",
            status="completed",
            summary="完成本次实验",
            candidate_ids=("B7", "C12"),
            observations=("边缘更平滑",),
            hypotheses=("宽度可能由映射范围导致",),
            recommendation="建议采用 C12",
        )
        state = add_result(state, result)
        records = read_task(state, "G1")
        self.assertEqual(records.results, (result,))
        self.assertEqual(records.baseline, self.state["candidates"]["B7"])

    def test_result_rejects_undeclared_candidates_from_other_tasks(self) -> None:
        state = add_candidate(self.state, CandidateRecord(id="unrelated", task_id="G0", code_path="unrelated.glsl"))
        result = ResultRecord(id="invalid", task_id="G1", status="completed", summary="引用无关候选", candidate_ids=("unrelated",))
        with self.assertRaisesRegex(ValueError, "outside task"):
            add_result(state, result)
        self.assertNotIn("invalid", state["results"])

    def test_result_requires_existing_task_and_candidate(self) -> None:
        results = (
            ResultRecord(id="invalid", task_id="missing", status="blocked", summary="没有任务"),
            ResultRecord(id="invalid", task_id="G1", status="partial", summary="没有候选", candidate_ids=("missing",)),
        )
        for result in results:
            with self.subTest(result=result), self.assertRaises(KeyError):
                add_result(self.state, result)
        self.assertNotIn("invalid", self.state["results"])

    def test_review_and_followup_analysis_share_explicit_evidence(self) -> None:
        review = TaskRecord(id="V1", role="review", target_version="T1", objective="检查基线", candidate_ids=("B7",))
        state = add_task(self.state, review)
        result = ResultRecord(id="R1", task_id="V1", status="completed", summary="倒影偏宽", candidate_ids=("B7",))
        state = add_result(state, result)
        followup = TaskRecord(id="A1", role="analysis", target_version="T1", objective="规划实验", related_result_ids=("R1",))
        state = add_task(state, followup)
        self.assertEqual(read_task(state, "A1").related_results, (result,))
        self.assertEqual(read_task(state, "G1").related_results, ())
        analysis = ResultRecord(id="R2", task_id="A1", status="completed", summary="根据评审规划实验", candidate_ids=("B7",))
        state = add_result(state, analysis)
        self.assertEqual(read_task(state, "A1").results, (analysis,))

    def test_new_target_can_explicitly_reassess_an_old_candidate(self) -> None:
        state = add_target(self.state, TargetRecord(version="T2", request="修改目标后重新评审", reference_path="inputs/reference.png"))
        task = TaskRecord(id="V2", role="review", target_version="T2", objective="重新评价旧版", candidate_ids=("B7",))
        state = add_task(state, task)
        records = read_task(state, "V2")
        self.assertEqual(records.target.version, "T2")
        origin = state["tasks"][records.inputs[0].task_id]
        self.assertEqual(origin.target_version, "T1")

    def test_partial_and_completed_results_preserve_both_submissions(self) -> None:
        partial = ResultRecord(id="R1", task_id="G1", status="partial", summary="尚未完成", limitations=("缺少预览",))
        completed = ResultRecord(id="R2", task_id="G1", status="completed", summary="本次任务完成")
        state = add_result(self.state, partial)
        state = add_result(state, completed)
        self.assertEqual(read_task(state, "G1").results, (partial, completed))
