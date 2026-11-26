"""单次全图规划的固定事实、唯一草稿及接受终态."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Literal

from shader_deep.agents.draft_runtime import DraftReadTracker
from shader_deep.domain.scene_planning import ScenePlanningSubmission, validate_scene_planning
from shader_deep.infrastructure.storage.json_draft import DraftError
from shader_deep.infrastructure.storage.planning_draft import DRAFT_PATH, PlanningDraftStore
from shader_deep.infrastructure.storage.planning_inputs import reference_size
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.planning.contracts import ScenePlanningResult

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.scene_planning import ObservationRevisionRequest
    from shader_deep.infrastructure.storage.planning_inputs import CapturedPlanningSource
    from shader_deep.workflows.planning.cascade import PlanningRevision
    from shader_deep.workflows.planning.options import PlanningOptions


class PlanningSession:
    """会话锁串行保护版本、接受和终态, SDK 持有模型历史."""

    def __init__(
        self,
        source: CapturedPlanningSource,
        options: PlanningOptions,
        *,
        revision_feedback: tuple[str, ...] = (),
        revision: PlanningRevision | None = None,
    ) -> None:
        """绑定本次独立运行捕获的输入."""
        self.source, self.options, self.directory = source, options, source.directory
        if (self.directory / "manifest.json").exists():
            msg = "规划不续跑已有目录, 请创建新运行"
            raise ValueError(msg)
        self.reference_url = source.reference_url
        self.revision_feedback = revision_feedback
        self.revision = revision
        self.submission: ScenePlanningSubmission | None = None
        self.state: Literal["running", "accepted", "stopped"] = "running"
        self.reason: str | None = None
        self.observation_revision_request: ObservationRevisionRequest | None = None
        self.model_calls = 0
        self.validation_errors = 0
        self.tool_refusals = 0
        self.runtime_metadata: dict[str, JsonValue] = {}
        self.resource_read_results: list[dict[str, str]] = []
        self._lock = RLock()
        self.draft = PlanningDraftStore(self.directory, source.observations, lock=self._lock)
        if revision is not None:
            self.draft.create(revision.previous)
        self._reads = DraftReadTracker(self.draft, finish_tool="finish_planning")
        self.request_revision: str | None = None
        self.accepted_revision: str | None = None
        self.last_edit: dict[str, JsonValue] = {}
        (self.directory / "results").mkdir(parents=True, exist_ok=True)
        self.save_manifest()

    @property
    def finished(self) -> bool:
        """返回图是否应该在摘要及下次模型调用之前结束."""
        with self._lock:
            return self.state != "running"

    @property
    def seen_revision(self) -> str | None:
        """返回完整正文已实际送入主请求的版本."""
        return self._reads.seen_revision

    def context(self) -> dict[str, JsonValue]:
        """构造同一固定输入及本轮预算, 由请求级中间件注入."""
        with self._lock:
            anchors = self.source.observation_manifest.reference_measurements
            return {
                "user_request": self.source.request,
                "scope": "whole_scene",
                "observation_sha256": self.source.observation_sha256,
                "observations": self.source.observations.model_dump(mode="json"),
                "context_notes": list(self.source.observation_manifest.context_notes),
                "open_questions": [item.model_dump(mode="json") for item in self.source.observation_manifest.open_questions],
                "observation_gaps": [item.model_dump(mode="json") for item in self.source.observation_manifest.gaps],
                "reference_measurements": [item.model_dump(mode="json", exclude_none=True) for item in anchors],
                **({"revision_feedback": list(self.revision_feedback)} if self.revision_feedback else {}),
                **(
                    {
                        "affected_observation_ids": list(self.revision.affected),
                        "revision_instruction": "已预填旧稿, 回读并仅 edit_file 修订受影响的目标与组合。保留其余目标和共享机制。",
                    }
                    if self.revision
                    else {}
                ),
                "generation_constraints": self.source.constraints.model_dump(mode="json"),
                "draft_revision": self.draft.revision,
                "state": self.state,
                "model_calls_remaining": max(0, self.options.max_model_calls - self.model_calls),
            }

    def execution(self) -> dict[str, JsonValue]:
        """返回不含模型消息、凭据及图像载荷的运行事实."""
        with self._lock:
            return {
                "planning_protocol": "scene_planning_v1",
                "execution": "deep_agents_single_planner",
                "state": self.state,
                "reason": self.reason,
                **self._revision_details(),
                "model_calls": self.model_calls,
                "max_model_calls": self.options.max_model_calls,
                "validation_errors": self.validation_errors,
                "tool_refusals": self.tool_refusals,
                "observation_sha256": self.source.observation_sha256,
                "reference_sha256": self.source.observation_manifest.reference.sha256,
                "draft_path": "draft/planning.json" if self.draft.revision else None,
                "draft_revision": self.draft.revision,
                "seen_revision": self.seen_revision,
                "accepted_revision": self.accepted_revision,
                "accepted_snapshot": "results/planning.json" if self.submission else None,
                "resource_read_results": [dict(item) for item in self.resource_read_results],
                **self.runtime_metadata,
            }

    def record_runtime(self, metadata: dict[str, JsonValue]) -> None:
        """合并由程序提取的配置身份与实际成本."""
        with self._lock:
            self.runtime_metadata.update(metadata)

    def record_rejection(self, *, validation: bool) -> None:
        """分别计数合同拒绝及工具执行准入拒绝."""
        with self._lock:
            if validation:
                self.validation_errors += 1
            else:
                self.tool_refusals += 1

    def record_resource_read(self, path: str, status: Literal["success", "error"]) -> None:
        """记录受控规则资源的实际工具结果."""
        with self._lock:
            self.resource_read_results.append({"path": path, "status": status})

    def save_manifest(self) -> None:
        """将当前业务状态保存为单文件快照."""
        write_snapshot(self.directory / "manifest.json", self.execution())

    def begin_model_call(self) -> bool:
        """终态先于计数, 逻辑预算耗尽后结束图."""
        with self._lock:
            if self.state != "running":
                return False
            if self.model_calls >= self.options.max_model_calls:
                self.state, self.reason = "stopped", "model_call_limit"
                return False
            self.model_calls += 1
            return True

    def bind_request(self) -> None:
        """绑定产生下一次工具响应的草稿版本."""
        with self._lock:
            self.request_revision = self.draft.revision

    def _require_running(self) -> None:
        if self.state != "running":
            msg = "会话已结束"
            code = "session_finished"
            raise DraftError(code, msg, path=DRAFT_PATH)

    def save_draft(self, report: ScenePlanningSubmission) -> dict[str, JsonValue]:
        """首次创建草稿并返回可定位的业务问题."""
        with self._lock:
            self._require_running()
            return self.draft.create(report)

    def edit_draft(self, old: str, new: str, *, replace_all: bool = False) -> dict[str, JsonValue]:
        """根据当前请求版本局部修改, 编辑后失效已见版本."""
        with self._lock:
            self._require_running()
            self.last_edit = self.draft.edit(old, new, expected_revision=self.request_revision, replace_all=replace_all)
            if self.last_edit.get("before_revision") != self.last_edit.get("revision"):
                self._reads.invalidate()
            return self.last_edit

    def record_draft_read(self, identity: str, content: str) -> None:
        """登记当前版本实际返回的草稿行范围."""
        with self._lock:
            self._reads.record(identity, content)

    def present_reads(self, messages: list[object]) -> None:
        """在实际主请求成功后累计正文可见回执."""
        with self._lock:
            self._reads.present(messages)

    def finish_draft(self) -> dict[str, JsonValue]:
        """校验最新已见版本并冻结接受, 不接收再次提交的正文."""
        with self._lock:
            self._require_running()
            text, revision = self.draft.read(expected_revision=self.request_revision)
            if self.seen_revision != revision:
                msg = self._reads.unseen_message(text, revision)
                code = "draft_unseen"
                raise DraftError(code, msg, path=DRAFT_PATH)
            report = self.draft.report(expected_revision=revision)
            try:
                validate_scene_planning(report, self.source.observations)
                if self.revision is not None:
                    self.revision.validate(report)
            except ValueError as error:
                code = "invalid_planning"
                raise DraftError(code, str(error), path=DRAFT_PATH) from error
            self._accept(report, revision)
            return {"accepted": True, "snapshot": "results/planning.json", "accepted_revision": revision}

    def _accept(self, report: ScenePlanningSubmission, revision: str) -> None:
        frozen = ScenePlanningSubmission.model_validate(report.model_dump(mode="json", exclude_none=True))
        try:
            write_snapshot(
                self.directory / "results/planning.json",
                {"submission": frozen.model_dump(mode="json"), "observation_sha256": self.source.observation_sha256},
            )
        except OSError:
            self.state, self.reason = "stopped", "snapshot_error"
            raise
        self.submission, self.accepted_revision, self.state = frozen, revision, "accepted"
        self.save_manifest()

    def _revision_details(self) -> dict[str, JsonValue]:
        if self.observation_revision_request is None:
            return {}
        return {"observation_revision_request": self.observation_revision_request.model_dump(mode="json")}

    def stop(self, reason: str, *, observation_revision_request: ObservationRevisionRequest | None = None) -> dict[str, JsonValue]:
        """保存明确受阻原因, 草稿留在本轮目录."""
        if not reason.strip():
            msg = "停止原因不能为空"
            raise ValueError(msg)
        with self._lock:
            self._require_running()
            if observation_revision_request is not None:
                observation_revision_request.validate_against(self.source.observations, reference_size(self.source.reference_bytes))
            self.observation_revision_request = observation_revision_request
            self.state, self.reason = "stopped", reason
            self.save_manifest()
            return {"stopped": True, "reason": reason, **self._revision_details()}

    def finalize(self, error: str | None = None) -> ScenePlanningResult:
        """根据接受事实和两类必要缺口生成最终核查结果."""
        with self._lock:
            if self.state == "running":
                self.state, self.reason = "stopped", error or "Agent 结束时未完成合法规划"
            complete = (
                self.submission is not None and not self.source.observation_manifest.gaps and any(not item.gaps for item in self.submission.sketches)
            )
            status = "failed" if self.submission is None else "completed" if complete else "partial"
            return ScenePlanningResult(
                status=status,
                submission=self.submission,
                error=error or self.reason,
                execution=self.execution(),
                observation_revision_request=self.observation_revision_request,
            )
