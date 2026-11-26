"""单次全图观察的固定输入、接受快照与终态, 不持有模型历史."""

from __future__ import annotations

import base64
import hashlib
import io
from threading import RLock
from typing import TYPE_CHECKING, Literal

from PIL import Image

from shader_deep.agents.draft_runtime import DraftReadTracker
from shader_deep.domain.scene_observations import SceneObservationSubmission, build_scene_observations
from shader_deep.infrastructure.storage.observation_draft import DraftError, DraftStore
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.analysis.contracts import SceneAnalysisResult
from shader_deep.workflows.analysis.measurements import ObservationMeasurements

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from shader_deep.domain.evidence import ImageRegion
    from shader_deep.domain.scene_observations import ReferenceMeasurement, SceneObservations
    from shader_deep.workflows.options import AnalysisOptions


class AnalysisSession:
    """会话锁串行保护草稿操作和接受, 模型与网络请求在锁外执行."""

    def __init__(
        self,
        request: str,
        reference_url: str,
        directory: Path,
        options: AnalysisOptions,
        *,
        revision_context: dict[str, JsonValue] | None = None,
        inherited_measurements: tuple[ReferenceMeasurement, ...] = (),
    ) -> None:
        """固定图像与要求, 拒绝复用已有运行目录."""
        if not request.strip() or not reference_url.startswith("data:image/png;base64,"):
            msg = "观察分析需要非空要求与固定 PNG"
            raise ValueError(msg)
        if (directory / "manifest.json").exists():
            msg = "分析不续跑已有目录, 请创建新运行"
            raise ValueError(msg)
        reference = base64.b64decode(reference_url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(reference)) as image:
            self.width, self.height = image.size
        self.request, self.reference_url, self.directory, self.options = request, reference_url, directory, options
        self.reference_sha256 = hashlib.sha256(reference).hexdigest()
        self._measurements = ObservationMeasurements(reference, directory, self.reference_sha256, inherited=inherited_measurements)
        self._revision_context = revision_context
        self.submission: SceneObservationSubmission | None = None
        self.observations: SceneObservations | None = None
        self.state: Literal["running", "accepted", "stopped"] = "running"
        self.reason: str | None = None
        self.model_calls = 0
        self.validation_errors = 0
        self.tool_refusals = 0
        self.runtime_metadata: dict[str, JsonValue] = {}
        self.resource_read_results: list[dict[str, str]] = []
        self._saving = False
        self._lock = RLock()
        self.draft = DraftStore(directory, lock=self._lock)
        self.request_revision: str | None = None
        self._reads = DraftReadTracker(self.draft, finish_tool="finish_analysis")
        self.last_edit: dict[str, JsonValue] = {}
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "results").mkdir(exist_ok=True)
        self.save_manifest()

    @property
    def seen_revision(self) -> str | None:
        """返回已完整进入实际模型请求的草稿版本."""
        return self._reads.seen_revision

    @property
    def finished(self) -> bool:
        """返回是否应在下一次模型包装链之前结束."""
        with self._lock:
            return self.state != "running"

    def context(self) -> dict[str, JsonValue]:
        """只返回固定业务事实, 不保存图像 data URL 或模型历史."""
        with self._lock:
            return {
                "user_request": self.request,
                "scope": "whole_scene",
                "draft_revision": self.draft.revision,
                "image_size": {"width": self.width, "height": self.height},
                "state": self.state,
                "model_calls_remaining": max(0, self.options.max_model_calls - self.model_calls),
                **({"observation_revision": self._revision_context} if self._revision_context is not None else {}),
            }

    def measure_reference(self, kind: Literal["crop", "region_stats"], region: ImageRegion, question: str) -> dict[str, JsonValue]:
        """允许在本轮活动期间读取固定原图的局部像素."""
        with self._lock:
            self._require_running()
            return self._measurements.measure(kind, region, question)

    def crop_images(self) -> tuple[tuple[str, str], ...]:
        """提供已采集裁图及对应锚点 ID, 供请求级注入."""
        with self._lock:
            return self._measurements.crop_images

    def execution(self) -> dict[str, JsonValue]:
        """生成可保存的运行事实, 不把弃用角色参数当成实际执行数量."""
        with self._lock:
            return {
                "analysis_protocol": "scene_observations_v1",
                "execution": "deep_agents_single_observer",
                "state": self.state,
                "reason": self.reason,
                "model_calls": self.model_calls,
                "validation_errors": self.validation_errors,
                "tool_refusals": self.tool_refusals,
                "max_model_calls": self.options.max_model_calls,
                "reference_sha256": self.reference_sha256,
                "draft_path": "draft/observations.json" if self.draft.revision else None,
                "draft_revision": self.draft.revision,
                "seen_revision": self.seen_revision,
                "accepted_revision": self.draft.revision if self.submission else None,
                "accepted_snapshot": "results/observations.json" if self.submission else None,
                "resource_read_results": [dict(item) for item in self.resource_read_results],
                **self.runtime_metadata,
            }

    def record_runtime(self, metadata: dict[str, JsonValue]) -> None:
        """记录程序计算的身份与成本信息, 不存模型消息或凭据."""
        with self._lock:
            self.runtime_metadata.update(metadata)

    def record_rejection(self, *, validation: bool) -> None:
        """分别计数合同修复与被执行门禁拒绝的工具调用."""
        with self._lock:
            if validation:
                self.validation_errors += 1
            else:
                self.tool_refusals += 1

    def record_resource_read(self, path: str, status: Literal["success", "error"]) -> None:
        """记录受控 skill 文件的工具返回状态, 不存正文或模型历史.

        Args:
            path: 角色准入层已验证的虚拟资源路径.
            status: SDK 工具返回状态; 成功不代表模型理解了内容.
        """
        with self._lock:
            self.resource_read_results.append({"path": path, "status": status})

    def save_manifest(self) -> None:
        """将业务运行事实保存为单文件快照."""
        write_snapshot(self.directory / "manifest.json", self.execution())

    def begin_model_call(self) -> bool:
        """终态先于计数; 用尽预算后结束而不请求模型."""
        with self._lock:
            if self.state != "running":
                return False
            if self.model_calls >= self.options.max_model_calls:
                self.state, self.reason = "stopped", "model_call_limit"
                return False
            self.model_calls += 1
            return True

    def bind_request(self) -> None:
        """绑定产生下一工具响应的草稿版本."""
        self.request_revision = self.draft.revision

    def save_draft(self, report: SceneObservationSubmission) -> dict[str, JsonValue]:
        """首次保存结构合法草稿, 业务引用问题作为诊断返回."""
        with self._lock:
            self._require_running()
            return self.draft.create(report)

    def edit_draft(self, old: str, new: str, *, replace_all: bool = False) -> dict[str, JsonValue]:
        """按本次响应绑定版本修改唯一草稿片段."""
        with self._lock:
            self._require_running()
            self.last_edit = self.draft.edit(old, new, expected_revision=self.request_revision, replace_all=replace_all)
            if self.last_edit.get("before_revision") != self.last_edit.get("revision"):
                self._reads.invalidate()
            return self.last_edit

    def _require_running(self) -> None:
        if self.state != "running":
            msg = "session_finished: 会话已结束"
            code = "session_finished"
            raise DraftError(code, msg)

    def record_draft_read(self, identity: str, content: str) -> None:
        """只登记实际工具正文完整包含的同版本行范围."""
        with self._lock:
            self._reads.record(identity, content)

    def present_reads(self, messages: list[object]) -> None:
        """成功主请求中确实包含回读结果后更新已见版本."""
        with self._lock:
            self._reads.present(messages)

    def _unseen_message(self, text: str, revision: str) -> str:
        return self._reads.unseen_message(text, revision)

    def finish_draft(self) -> dict[str, JsonValue]:
        """核查最新全文已进入主请求后冻结并接受同一版本."""
        with self._lock:
            self._require_running()
            text, revision = self.draft.read(expected_revision=self.request_revision)
            if self.seen_revision != revision:
                msg = self._unseen_message(text, revision)
                code = "draft_unseen"
                raise DraftError(code, msg)
            try:
                result = self._submit(self.draft.report(expected_revision=revision))
            except ValueError as error:
                code = "invalid_observations"
                raise DraftError(code, str(error)) from error
            return {**result, "accepted_revision": revision}

    def _reserve(self) -> None:
        with self._lock:
            if self.state != "running" or self._saving:
                msg = "当前会话不能接受新的观察提交"
                raise ValueError(msg)
            self._saving = True

    def _submit(self, report: SceneObservationSubmission) -> dict[str, JsonValue]:
        """验证完整观察并在快照成功写入后接受, 保留原短 ID."""
        frozen = report.model_copy(deep=True)
        observations = build_scene_observations(frozen)
        anchors = self._measurements.anchors
        self._reserve()
        try:
            write_snapshot(
                self.directory / "results" / "observations.json",
                {
                    "submission": frozen.model_dump(mode="json"),
                    "observations": observations.model_dump(mode="json"),
                    **({"reference_measurements": [item.model_dump(mode="json") for item in anchors]} if anchors else {}),
                },
            )
            self._accept(frozen, observations)
        except OSError:
            with self._lock:
                self.state, self.reason = "stopped", "snapshot_error"
            raise
        finally:
            with self._lock:
                self._saving = False
        self.save_manifest()
        return {"accepted": True, "snapshot": "results/observations.json"}

    def _accept(self, report: SceneObservationSubmission, observations: SceneObservations) -> None:
        with self._lock:
            if self.state != "running":
                msg = "写入快照期间会话已结束, 候选不登记为接受结果"
                raise ValueError(msg)
            self.submission, self.observations, self.state = report, observations, "accepted"

    def stop(self, reason: str) -> dict[str, JsonValue]:
        """保存明确受阻原因, 不把自然语言结束当成合法观察."""
        if not reason.strip():
            msg = "停止原因不能为空"
            raise ValueError(msg)
        with self._lock:
            if self.state != "running":
                msg = "会话已结束"
                raise ValueError(msg)
            self.state, self.reason = "stopped", reason
        self.save_manifest()
        return {"stopped": True, "reason": reason}

    def finalize(self, error: str | None = None) -> SceneAnalysisResult:
        """根据接受事实核查结果, 图异常不抹去已有合法观察."""
        with self._lock:
            if self.state == "running":
                self.state, self.reason = "stopped", error or "Agent 结束时未提交合法观察"
            status = "failed" if self.submission is None else "partial" if self.submission.gaps else "completed"
            return SceneAnalysisResult(
                status=status,
                submission=self.submission,
                observations=self.observations,
                error=error or self.reason,
                task_state=self.execution(),
                reference_measurements=self._measurements.anchors,
            )

    def save_result(self, result: SceneAnalysisResult) -> None:
        """保存最终核查状态, 发布回调可在失败时更新它."""
        write_snapshot(
            self.directory / "result.json",
            {"analysis_protocol": "scene_observations_v1", "status": result.status, "error": result.error, "execution": result.task_state},
        )
