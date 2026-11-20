"""单次评审的固定事实、有限预算与接受终态."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Literal

from shader_deep.domain.review_coverage import complete_coverage, validate_coverage
from shader_deep.domain.visual_review import derive_verdict, validate_review_submission
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.review.contracts import ReviewResult

if TYPE_CHECKING:
    from pydantic import JsonValue

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
                "review_protocol": "visual_review_v1",
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
            }

    def record_runtime(self, values: dict[str, JsonValue]) -> None:
        """合并程序提取的安全配置与实际用量."""
        with self._lock:
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
            report = complete_coverage(report, self.source.criteria, self.source.coverage)
            validate_review_submission(report, self.source.criteria)
            validate_coverage(report, self.source.criteria, self.source.coverage)
            verdict = derive_verdict(report)
            write_snapshot(self.directory / "results" / "accepted.json", report.model_dump(mode="json"))
            self.submission, self.state = report, "accepted"
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
