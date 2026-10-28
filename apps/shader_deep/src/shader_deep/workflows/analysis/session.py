"""原生图之外的本轮业务事实, 不保存消息或恢复执行状态."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from dataclasses import asdict, dataclass
from threading import RLock
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue, TypeAdapter

from shader_deep.domain.analysis import TargetPlan
from shader_deep.domain.five_libraries import (
    Element,
    ExplorationSubmission,
    FiveLibraries,
    Issue,
    MergeProposal,
    apply_merges,
    collect_explorations,
    exploration_mappings,
    merge_mapping,
    rewrite_issues,
    validate_exploration,
)
from shader_deep.domain.five_libraries.references import rewrite_text
from shader_deep.imaging.focus import validate_target_box
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.workflows.analysis.contracts import FiveAnalysisResult

if TYPE_CHECKING:
    from pathlib import Path

    from shader_deep.domain.evidence import ImageRegion
    from shader_deep.workflows.options import AnalysisOptions


@dataclass
class TaskSlot:
    """固定业务槽位, 只有一次启动和一次合法提交."""

    # pending 经准入变为 running; 合法提交才是 succeeded, 失败或未提交结束均为 failed.
    # succeeded/failed 不再重开, SDK 子图的自然语言结束不决定这里的成功状态.
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    error: str | None = None
    report: ExplorationSubmission | None = None
    materials: dict[str, JsonValue] | None = None


class AnalysisSession:
    """锁只保护业务准入和提交, 不执行模型或自建调度."""

    def __init__(self, request: str, reference_url: str, directory: Path, options: AnalysisOptions) -> None:
        """固定本轮原图、要求和容量, 已有原生 manifest 不支持续跑."""
        if not request.strip() or not reference_url.startswith("data:image/png;base64,") or options.max_tasks not in {2, 3}:
            msg = "原生分析需要非空要求、固定 PNG 和两个或三个探索槽位"
            raise ValueError(msg)
        if (directory / "manifest.json").exists():
            msg = "原生分析不续跑已有目录, 请创建新运行"
            raise ValueError(msg)
        self.request, self.reference_url, self.directory, self.options = request, reference_url, directory, options
        self.elements: tuple[Element, ...] = ()
        self.plan: TargetPlan | None = None
        # 会话只持有固定输入及已接受的业务事实; 历史、todos 和 scratch 由 SDK 图状态管理.
        self.slots: dict[str, TaskSlot] = {}
        self.versions: dict[str, dict[str, JsonValue]] = {}
        # 准入/提交会嵌套调用回执与快照, 因而需要可重入锁; 模型调用在会话锁之外.
        self._lock = RLock()
        self._closed = False
        self._baseline_error: str | None = None
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "results").mkdir(exist_ok=True)
        self._save_manifest()

    def _save_manifest(self) -> None:
        """仅保存业务索引; 图状态、密钥和图像 data URL 不进入 manifest."""
        reference = base64.b64decode(self.reference_url.split(",", 1)[1], validate=True)
        write_snapshot(
            self.directory / "manifest.json",
            {
                "analysis_protocol": "five_libraries_v1",
                "execution": "deep_agents_native",
                "user_request": self.request,
                "reference_sha256": hashlib.sha256(reference).hexdigest(),
                "elements": [item.model_dump(mode="json") for item in self.elements],
                "plan": self.plan.model_dump(mode="json") if self.plan else None,
                "tasks": {role: self.receipt(role) for role in self.slots},
                "versions": list(self.versions),
                "closed": self._closed,
                "baseline_error": self._baseline_error,
            },
        )

    def submit_elements(self, elements: tuple[Element, ...]) -> dict[str, JsonValue]:
        """登记唯一元素目录, 已接受内容只能幂等回读."""
        with self._lock:
            identities = [item.id for item in elements]
            if self._closed or not elements or len(set(identities)) != len(elements) or any(item.feature_ids for item in elements):
                msg = "元素目录必须非空、ID 唯一且不含共同特征初稿"
                raise ValueError(msg)
            if self.elements and self.elements != elements:
                msg = "已接受元素目录不可改写"
                raise ValueError(msg)
            self.elements = elements
            self._save_manifest()
            return {"status": "accepted", "result_id": "elements", "elements": [item.model_dump(mode="json") for item in elements]}

    def freeze_plan(self, target_element_id: str, directions: tuple[str, ...], target_box: ImageRegion | None = None) -> dict[str, JsonValue]:
        """一次冻结目标与本批方向; 原生 task 只能使用已登记槽位."""
        with self._lock:
            plan = TargetPlan(elements=self.elements, target_element_id=target_element_id, directions=directions, target_box=target_box)
            validate_target_box(self.reference_url, target_box)
            if self._closed or len(directions) != self.options.max_tasks or (self.plan is not None and plan != self.plan):
                msg = "本批目标与固定数量方向不能在冻结后改写"
                raise ValueError(msg)
            if self.plan is None:
                self.plan = plan
                # 先确定全部合法身份, 后续 task 只能领取这些槽位, 不能临时增加方向.
                self.slots = {f"explorer-{number}": TaskSlot() for number in range(1, len(directions) + 1)}
                self.slots["integrator"] = TaskSlot()
                self._save_manifest()
            return {"status": "accepted", "plan": plan.model_dump(mode="json"), "tasks": list(self.slots)}

    def _slot(self, role: str) -> TaskSlot:
        if self.plan is None or role not in self.slots:
            msg = "task 角色必须属于本轮已冻结的探索或整合槽位"
            raise ValueError(msg)
        return self.slots[role]

    def admit_task(self, role: str) -> bool:
        """与状态变更原子核对, 重复调用不重跑已启动或终结方向."""
        with self._lock:
            slot = self._slot(role)
            # 检查与 pending -> running 同处临界区, 并发重复 task 也只能有一个启动者.
            if self._closed or slot.status != "pending":
                return False
            if role == "integrator":
                # 失败探索也算本批终态; 仅将已合法提交的来源汇成可用 V0 后才准入整合.
                if any(item.status in {"pending", "running"} for name, item in self.slots.items() if name != "integrator"):
                    msg = "本批各探索必须先形成成功或失败终态, 才能整合"
                    raise ValueError(msg)
                self._collect_baseline()
                if "V0" not in self.versions:
                    msg = "没有合法 V0 可供整合"
                    raise ValueError(msg)
            slot.status = "running"
            self._save_manifest()
            return True

    def is_active(self, role: str) -> bool:
        """已接受提交或失败后撤销新的业务写入权."""
        with self._lock:
            return not self._closed and self._slot(role).status == "running"

    def fail_task(self, role: str, error: str) -> None:
        """失败形成终态, 已接受结果不被后续图异常覆盖."""
        with self._lock:
            slot = self._slot(role)
            # 已成功提交的结果先于后续图异常成立, 不能被清理路径降级成失败.
            if slot.status in {"pending", "running"}:
                slot.status, slot.error = "failed", error
                self._save_manifest()

    def complete_task(self, role: str) -> None:
        """模型普通结束而未合法提交时记录失败."""
        self.fail_task(role, "子 agent 结束时未提交合法业务结果")

    def receipt(self, role: str) -> dict[str, JsonValue]:
        """Task 回执只读取程序事实, 不解释模型结束文本."""
        with self._lock:
            slot = self._slot(role)
            return {
                "task_id": role,
                "status": slot.status,
                "error": slot.error,
                "result_id": ("V1" if role == "integrator" else role) if slot.status == "succeeded" else None,
            }

    def fixed_input(self, role: str) -> dict[str, JsonValue]:
        """按角色投影已冻结输入; 探索不含兄弟报告和父图状态."""
        with self._lock:
            self._slot(role)
            if self.plan is None:
                msg = "业务计划尚未冻结"
                raise ValueError(msg)
            if role == "integrator":
                if "V0" not in self.versions:
                    msg = "整合输入需要已发布的固定 V0"
                    raise ValueError(msg)
                # 子图取得独立副本, 即使修改嵌套字典也不能反写已发布的基线.
                return {"target_element_id": self.plan.target.id, "libraries": deepcopy(self.versions["V0"]["libraries"]), "input_version": "V0"}
            number = int(role.removeprefix("explorer-")) - 1
            return {
                "user_request": self.request,
                "reference_url": self.reference_url,
                "target": self.plan.target.model_dump(mode="json"),
                "direction": self.plan.directions[number],
                "target_box": TypeAdapter(JsonValue).validate_python(asdict(self.plan.target_box)) if self.plan.target_box else None,
            }

    def submit_exploration(self, role: str, report: ExplorationSubmission, *, materials: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        """领域校验通过后保存合法来源, 不允许同槽位覆盖结果."""
        with self._lock:
            slot = self._slot(role)
            if role == "integrator" or not self.is_active(role):
                msg = "当前槽位无探索提交权限"
                raise ValueError(msg)
            if self.plan is None:
                msg = "业务计划尚未冻结"
                raise ValueError(msg)
            validate_exploration(report, self.plan.target)
            if report.evidence_links and materials is None:
                msg = "引用证据需要当前角色已验证并冻结的测量材料"
                raise ValueError(msg)
            write_snapshot(self.directory / "results" / (role + ".json"), report.model_dump(mode="json"))
            # 冻结报告和嵌套证据材料, 防止调用方持有的可变对象在接受后悄悄改写来源.
            frozen = deepcopy(materials or {})
            write_snapshot(self.directory / "results" / (role + "-materials.json"), frozen)
            slot.report, slot.materials, slot.status = report.model_copy(deep=True), frozen, "succeeded"
            self._save_manifest()
            return self.receipt(role)

    def _materials(self, mappings: dict[str, dict[str, str]]) -> dict[str, JsonValue]:
        notes: list[str] = []
        records: list[JsonValue] = []
        links: dict[str, list[str]] = {}
        assets: dict[str, JsonValue] = {}
        contents: dict[str, JsonValue] = {}
        for role, mapping in mappings.items():
            slot = self.slots[role]
            if slot.report is None:
                msg = "来源映射缺少已接受探索产物"
                raise ValueError(msg)
            material = slot.materials or {}
            # 业务项 ID 随汇集/合并改写, 测量记录与资产仍保留原始证据身份.
            notes.extend(rewrite_text(note, mapping) for note in slot.report.context_notes)
            records.extend(TypeAdapter(list[JsonValue]).validate_python(material.get("records", [])))
            for key, value in slot.report.evidence_links.items():
                links.setdefault(mapping[key], []).extend(value)
            assets.update(TypeAdapter(dict[str, JsonValue]).validate_python(material.get("assets", {})))
            contents.update(TypeAdapter(dict[str, JsonValue]).validate_python(material.get("asset_contents", {})))
        return {
            "context_notes": list(dict.fromkeys(notes)),
            "records": records,
            # 多个来源合到同一业务项时累积全部证据, 只去除重复链接而不覆盖旧来源.
            "links": {key: list(dict.fromkeys(value)) for key, value in links.items()},
            "assets": assets,
            "asset_contents": contents,
        }

    def _publish_version(self, version: str, payload: dict[str, JsonValue]) -> None:
        write_snapshot(self.directory / "results" / (version + ".json"), payload)
        self.versions[version] = payload
        self._save_manifest()

    def _collect_baseline(self) -> None:
        # V0 一经发布便复用; 整合或最终核查重复触达此处不会重新编排来源 ID.
        if "V0" in self.versions or self.plan is None:
            return
        reports = {role: slot.report for role, slot in self.slots.items() if slot.report is not None}
        if not reports:
            return
        try:
            libraries, questions, gaps = collect_explorations(self.plan.target, reports)
        except ValueError as error:
            self._baseline_error = str(error)
            self._save_manifest()
            return
        missing = tuple(
            Issue(refs=(self.plan.target.id,), description=f"{role} 未完成: {slot.error or slot.status}")
            for role, slot in self.slots.items()
            if role != "integrator" and slot.status != "succeeded"
        )
        # 各探索的 F/R/M/S 是来源内短 ID; 同一稳定顺序生成包内映射, 同步重写材料引用.
        mappings = exploration_mappings(self.plan.target, reports)
        self._publish_version(
            "V0",
            {
                "libraries": libraries.model_dump(mode="json"),
                "open_questions": [item.model_dump(mode="json") for item in questions],
                "gaps": [item.model_dump(mode="json") for item in (*gaps, *missing)],
                "source_mappings": TypeAdapter(JsonValue).validate_python(mappings),
                "materials": self._materials(mappings),
            },
        )

    def submit_integration(self, proposal: MergeProposal) -> dict[str, JsonValue]:
        """仅合并既有 F/R/M, 草图与问题引用沿领域映射保义更新."""
        with self._lock:
            if not self.is_active("integrator"):
                msg = "当前槽位无整合提交权限"
                raise ValueError(msg)
            if self.plan is None:
                msg = "业务计划尚未冻结"
                raise ValueError(msg)
            baseline = self.versions["V0"]
            libraries = FiveLibraries.model_validate(baseline["libraries"])
            merged = apply_merges(libraries, proposal, self.plan.target.id)
            mapping = merge_mapping(libraries, proposal)
            old = TypeAdapter(dict[str, dict[str, str]]).validate_python(baseline["source_mappings"])
            # 串接“来源 ID -> V0 ID -> V1 保留 ID”, 让合并后的证据仍能追溯各探索.
            sources = {role: {key: mapping.get(value, value) for key, value in refs.items()} for role, refs in old.items()}
            payload: dict[str, JsonValue] = {
                "libraries": merged.model_dump(mode="json"),
                "source_mappings": TypeAdapter(JsonValue).validate_python(sources),
                "materials": self._materials(sources),
            }
            for key in ("open_questions", "gaps"):
                issues = TypeAdapter(tuple[Issue, ...]).validate_python(baseline[key])
                payload[key] = [item.model_dump(mode="json") for item in rewrite_issues(issues, mapping)]
            self._publish_version("V1", payload)
            self.slots["integrator"].status = "succeeded"
            self._save_manifest()
            return self.receipt("integrator")

    def read_analysis_result(self, result_id: str) -> dict[str, JsonValue]:
        """只回读已接受内容, 不提供任意目录或文件读取."""
        with self._lock:
            if result_id == "elements":
                result: JsonValue = [item.model_dump(mode="json") for item in self.elements]
            elif result_id in self.versions:
                # 回读只投影副本并省略资产正文, 保留内部版本供交付使用.
                payload = deepcopy(self.versions[result_id])
                materials = TypeAdapter(dict[str, JsonValue]).validate_python(payload["materials"])
                payload["materials"] = {key: value for key, value in materials.items() if key != "asset_contents"}
                result = payload
            else:
                slot = self._slot(result_id)
                if slot.report is None:
                    msg = "仅可读取本轮已接受的结果"
                    raise ValueError(msg)
                result = slot.report.model_dump(mode="json")
            return {"status": "succeeded", "result_id": result_id, "result": result}

    def context(self) -> dict[str, JsonValue]:
        """业务工具可重新取得目录、固定要求与缺口, 不依赖摘要记忆."""
        with self._lock:
            return {
                "user_request": self.request,
                "max_tasks": self.options.max_tasks,
                "reference_url": self.reference_url,
                "registered_elements": [item.model_dump(mode="json") for item in self.elements],
                "plan": self.plan.model_dump(mode="json") if self.plan else None,
                "tasks": [self.receipt(role) for role in self.slots],
                "available_versions": list(self.versions),
            }

    def finalize(self, error: str | None = None) -> FiveAnalysisResult:
        """一次图执行结束后核查实际产物, 保留合法 V0 与真实缺口."""
        with self._lock:
            # 先终结尚未合法提交的槽位, 再汇集已有来源; 没有提交的文本声明不会补成成功.
            for role in self.slots:
                self.fail_task(role, "主图结束, 槽位未完成合法提交")
            self._collect_baseline()
            # 优先合法 V1, 否则保留 V0; 图异常只追加缺口, 不抹去已经接受的报告.
            version = "V1" if "V1" in self.versions else "V0" if "V0" in self.versions else None
            result = self._selected_result(version, error)
            self._closed = True
            self._save_manifest()
            write_snapshot(
                self.directory / "result.json",
                {
                    "status": result.status,
                    "selected_version": version,
                    "target_element_id": result.target_element_id,
                    "open_questions": [item.model_dump(mode="json") for item in result.open_questions],
                    "gaps": [item.model_dump(mode="json") for item in result.gaps],
                },
            )
            return result

    def _selected_result(self, version: str | None, error: str | None) -> FiveAnalysisResult:
        target = self.plan.target.id if self.plan else None
        if version is None:
            return FiveAnalysisResult(
                target_element_id=target,
                libraries=None,
                status="failed",
                selected_version=None,
                gaps=(Issue(refs=(), description=error or self._baseline_error or "主图结束时未形成合法五库基线"),),
            )
        payload = self.versions[version]
        questions = TypeAdapter(tuple[Issue, ...]).validate_python(payload["open_questions"])
        gaps = TypeAdapter(tuple[Issue, ...]).validate_python(payload["gaps"])
        if version != "V1":
            reason = self.slots["integrator"].error or self.slots["integrator"].status
            gaps += (Issue(refs=(), description=f"整合未完成, 保留合法 V0: {reason}"),)
        if error:
            gaps += (Issue(refs=(), description=error),)
        return FiveAnalysisResult(
            target_element_id=target,
            libraries=FiveLibraries.model_validate(payload["libraries"]),
            status="partial" if gaps else "completed",
            selected_version=version,
            open_questions=questions,
            gaps=gaps,
        )
