"""单次评审的固定事实、有限预算与接受终态."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Literal

from shader_deep.domain.review_coverage import complete_coverage, validate_coverage
from shader_deep.domain.review_items import CheckRevision, bound_checks, summarize_checks, validate_check_answer
from shader_deep.domain.visual_review import ReviewValidationError, derive_verdict, validate_review_submission
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.review.contracts import ReviewResult
from shader_deep.workflows.review.publication import with_publication_seed

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.review_items import CheckAnswer
    from shader_deep.domain.visual_review import ReviewSubmission
    from shader_deep.infrastructure.storage.review_package import CapturedReviewSource
    from shader_deep.workflows.review.options import ReviewOptions


class ReviewSession:
    """锁保护工具批次共享的接受状态, 模型历史由 SDK 持有."""

    def __init__(self, source: CapturedReviewSource, options: ReviewOptions) -> None:
        """绑定本轮捕获输入与逻辑预算."""
        self.source, self.options, self.directory = source, options, source.directory
        if (self.directory / "manifest.json").exists():
            msg = "评审不续跑已有目录, 请创建新运行"
            raise ValueError(msg)
        self.state: Literal["running", "accepted", "stopped"] = "running"
        self.submission: ReviewSubmission | None = None
        self.checks = bound_checks(source.criteria, source.coverage) if source.request.protocol == "visual_review_v2" else ()
        self._answers: dict[str, CheckAnswer] = {}
        self._revisions: tuple[CheckRevision, ...] = ()
        self.reason: str | None = None
        self.model_calls = 0
        self.validation_errors = 0
        self.tool_refusals = 0
        self.runtime_metadata: dict[str, JsonValue] = {}
        self._lock = RLock()
        (self.directory / "results").mkdir(parents=True, exist_ok=True)
        self.save_manifest()

    @property
    def finished(self) -> bool:
        """返回是否应在下次模型及摘要前结束图."""
        with self._lock:
            return self.state != "running"

    def execution(self) -> dict[str, JsonValue]:
        """返回不含图像载荷、密钥或模型历史的执行事实."""
        with self._lock:
            return {
                "review_protocol": self.source.request.protocol,
                "state": self.state,
                "reason": self.reason,
                "model_calls": self.model_calls,
                "max_model_calls": self.options.max_model_calls,
                "validation_errors": self.validation_errors,
                "tool_refusals": self.tool_refusals,
                "criteria_sha256": self.source.criteria.criteria_sha256,
                "input_sha256": self.source.input_sha256,
                "accepted_snapshot": "results/accepted.json" if self.submission is not None else None,
                **self.runtime_metadata,
                **self._revision_metadata(self._revisions),
            }

    def record_runtime(self, values: dict[str, JsonValue]) -> None:
        """合并程序提取的安全配置与实际用量."""
        with self._lock:
            if "check_revisions" in values:
                msg = "逐项审计由重评工具生成"
                raise ValueError(msg)
            self.runtime_metadata.update(values)

    def record_rejection(self, *, validation: bool) -> None:
        """分别记录合同拒绝与工具权限拒绝."""
        with self._lock:
            if validation:
                self.validation_errors += 1
            else:
                self.tool_refusals += 1

    def save_manifest(self) -> None:
        """保存本轮执行事实的单文件快照."""
        write_snapshot(self.directory / "manifest.json", self.execution())

    def begin_model_call(self) -> bool:
        """终态与预算检查先于主模型请求."""
        with self._lock:
            if self.state != "running":
                return False
            if self.model_calls >= self.options.max_model_calls:
                self.state, self.reason = "stopped", "model_call_limit"
                return False
            self.model_calls += 1
            return True

    def _require_running(self) -> None:
        if self.state != "running":
            msg = "评审会话已结束"
            raise ValueError(msg)

    def submit(self, report: ReviewSubmission) -> dict[str, JsonValue]:
        """校验并保存接受快照, 写入成功后才登记终态."""
        with self._lock:
            self._require_running()
            if self.source.request.protocol != "visual_review_v1":
                msg = "此协议要求逐项登记后结束评审"
                raise ValueError(msg)
            report = complete_coverage(report, self.source.criteria, self.source.coverage)
            validate_review_submission(report, self.source.criteria)
            validate_coverage(report, self.source.criteria, self.source.coverage)
            return self._accept(report, report.model_dump(mode="json"), self._answers)

    def _ordered_answers(self, answers: dict[str, CheckAnswer]) -> tuple[CheckAnswer, ...]:
        return tuple(answers[check.check_id] for check in self.checks if check.check_id in answers)

    @property
    def check_answers(self) -> tuple[CheckAnswer, ...]:
        """返回冻结检查顺序内已登记的答案, 与同批声明次序解耦."""
        with self._lock:
            return self._ordered_answers(self._answers)

    @property
    def answer_revisions(self) -> dict[str, int]:
        """返回已登记项的当前版本, 供模型指定重评的起点."""
        with self._lock:
            return {**dict.fromkeys(self._answers, 1), **{item.after.check_id: item.revision for item in self._revisions}}

    @staticmethod
    def _revision_metadata(revisions: tuple[CheckRevision, ...]) -> dict[str, JsonValue]:
        return {"check_revisions": [revision.model_dump(mode="json") for revision in revisions]} if revisions else {}

    def _save_answer(self, answer: CheckAnswer, revisions: tuple[CheckRevision, ...]) -> None:
        updated = {**self._answers, answer.check_id: answer}
        ordered = [item.model_dump(mode="json") for item in self._ordered_answers(updated)]
        # 旧未重评快照保留列表外形; 首次重评起, 一次原子替换同时提交新答案和完整审计.
        snapshot = {"check_answers": ordered, **self._revision_metadata(revisions)} if revisions else ordered
        write_snapshot(self.directory / "results" / "check_answers.json", snapshot)
        self._answers, self._revisions = updated, revisions

    def _require_check_protocol(self) -> None:
        self._require_running()
        if self.source.request.protocol != "visual_review_v2":
            msg = "此协议只接受整份评审提交"
            raise ValueError(msg)

    def record_check(self, answer: CheckAnswer) -> dict[str, JsonValue]:
        """校验单项后写入答案快照, 拒绝后续工具改写已经登记的视觉判断."""
        with self._lock:
            self._require_check_protocol()
            validate_check_answer(answer, self.checks)
            previous = self._answers.get(answer.check_id)
            if previous is not None and previous != answer:
                code, msg = "check_already_answered", "该项已经登记, 保留原有答案"
                raise ReviewValidationError(code, f"/answers/{answer.check_id}", msg)
            if previous is None:
                self._save_answer(answer, self._revisions)
            return {"status": "recorded", "check_id": answer.check_id, "revision": self.answer_revisions[answer.check_id]}

    def revise_check(self, answer: CheckAnswer, expected_revision: int, reason: str, evidence: str) -> dict[str, JsonValue]:
        """按调用方看到的版本进行一次单项改判, 保存证据说明与前后答案."""
        with self._lock:
            self._require_check_protocol()
            validate_check_answer(answer, self.checks)
            change = CheckRevision.from_answer(
                self._answers.get(answer.check_id),
                answer,
                current=self.answer_revisions.get(answer.check_id, 0),
                expected=expected_revision,
                reason=reason,
                evidence=evidence,
                model_call=self.model_calls,
            )
            self._save_answer(answer, (*self._revisions, change))
            return {"status": "revised", "check_id": answer.check_id, "revision": change.revision}

    def finish(self, summary: str, limitations: tuple[str, ...] = (), *, answers: tuple[CheckAnswer, ...] = ()) -> dict[str, JsonValue]:
        """核对已登记项与本次补交的答案, 一并接受最终报告和证据."""
        with self._lock:
            self._require_check_protocol()
            # 先保留全部条目, 让领域校验看见与已登记项或同批新项冲突的重复 ID.
            combined = (*self._answers.values(), *answers)
            report = summarize_checks(self.source.criteria, self.source.coverage, combined, summary=summary, limitations=limitations)
            merged = {item.check_id: item for item in combined}
            snapshot = {
                "submission": report.model_dump(mode="json"),
                "check_answers": [item.model_dump(mode="json") for item in self._ordered_answers(merged)],
                **self._revision_metadata(self._revisions),
            }
            # 一次原子替换保存最终答案、审计和离线发布绑定. 正式发布凭据写入前退出也能恢复.
            return self._accept(report, with_publication_seed(self, snapshot), merged)

    def _accept(self, report: ReviewSubmission, snapshot: object, answers: dict[str, CheckAnswer]) -> dict[str, JsonValue]:
        verdict = derive_verdict(report)
        write_snapshot(self.directory / "results" / "accepted.json", snapshot)
        self._answers, self.submission, self.state = answers, report, "accepted"
        return {"status": "accepted", "verdict": verdict}

    def stop(self, reason: str) -> dict[str, JsonValue]:
        """记录非空受阻原因并结束会话."""
        with self._lock:
            self._require_running()
            if not reason.strip():
                msg = "停止评审需要非空原因"
                raise ValueError(msg)
            self.state, self.reason = "stopped", reason.strip()
            return {"status": "blocked", "reason": self.reason}

    def finalize(self, error: str | None = None) -> ReviewResult:
        """保留已接受结果, 无提交正常结束记录为受阻."""
        with self._lock:
            if self.submission is not None:
                return ReviewResult(status="completed", submission=self.submission, error=error)
            if error is not None:
                self.state, self.reason = "stopped", error
                return ReviewResult(status="failed", error=error)
            if self.state == "running":
                self.state, self.reason = "stopped", "no_submission"
            return ReviewResult(status="blocked", error=self.reason)
