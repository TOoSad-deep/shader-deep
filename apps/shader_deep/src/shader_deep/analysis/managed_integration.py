"""默认主整合: 有界发现小组, 受限执行, 独立复核初稿问题."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Literal, cast

from langchain.tools import tool
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from shader_deep.analysis.controlled import ControlledSession, ControlledWork
from shader_deep.analysis.exploration_session import ExplorationSession
from shader_deep.analysis.outline_review import OutlineReview, VerifyOutlineReview, require_issue_ids, review_error, revised_outline
from shader_deep.analysis.schemas import Text  # noqa: TC001  # Pydantic 在工具初始化时解析文本约束.
from shader_deep.analysis.submissions import SubmissionHandler
from shader_deep.analysis.types import AnalysisLimitError, AnalysisNoProgressError

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage

MIN_COMPARISON_TARGETS = 2

DISCOVERY_PROMPT = """根据本轮实际目录发现有具体依据的疑似重复项, 用 plan_comparisons 一次安排工作。
每组同类型、2 至 6 个明确目标, 说明具体问题; 本步骤只发现范围, 不执行合并或处置初稿问题。
不按 ID 名称猜测未提供内容。只安排有具体依据的疑似重复组, 不为凑数量列出所有组合。
同一阶段每个目标最多进入一组。候选只能在同一规范拥有者下比较, 已合并拥有者的候选和未合并拥有者的候选都可考虑。
6 是单组目标上限, 不是工作组数量上限。必要工作直接列入计划, 后端按全局调用和阶段额度执行并记录未执行项。
候选阶段提供已有拥有者比较结论; 已明确保留不同拥有者时, 不把强行合并这些拥有者列为候选比较的前置义务。
不同机制可并存, 待渲染验证点与没有修改其他类型对象本身不是未完成去重义务。
没有必要比较时 comparisons 为空并说明理由; 无法完成必要发现时写 deferred_work, 不冒充全库已去重。
目录完整提供或整阶段暂缓, 不存在需要自行翻页或扩大范围的工具。
"""

ISSUE_PROMPT = """核对本轮所有初稿问题及原图, 用 review_outline 提出处置和受限观察文字修订。
逐项给出 dismissed(反馈不成立)、revised(确有描述问题且可修订)、deferred(证据不足或超出允许修订范围)和依据。
只允许 element_features 替换已有元素的显著外观文字, relation_descriptions 按零基索引替换已有关系描述。
不改元素/实例身份、区域范围或关系端点, 不改探索报告和候选, 不把机制争议伪装为观察事实。
不确定观察可改为明确的歧义描述, 不强猜数量或遮挡。需要新增/拆分对象的反馈必须暂缓。
只有 revised 问题可附相关文字修订。每项反馈都要处置; 提案不会立即关闭问题, 还要独立请求复核。
"""

VERIFY_PROMPT = """根据原图、原初稿、修订后的初稿、完整库材料及反馈, 独立复核本轮问题处置提案。
对每个待复核 issue_id 调用 verify_outline, 给 confirmed 和具体理由。检查修订或驳回是否有依据、是否遗漏原反馈。
不要因为上一轮已提出修订就自动确认; 证据不够或修订未解决问题时 confirmed=false, 保留未完成义务。
本阶段不允许再修改文字, 也不裁决原图唯一实现机制。
"""


class PlannedComparison(BaseModel):
    """目录发现只决定同类目标与问题, 真实工作身份由程序生成."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["feature", "relation", "sketch"]
    target_ids: Annotated[tuple[Text, ...], Field(min_length=2, max_length=6)]
    question: Text


class PlannedCandidates(PlannedComparison):
    """候选发现不允许安排拥有者修改."""

    kind: Literal["candidate"]


class OwnerPlan(BaseModel):
    """拥有者目录的有限计划, 未选条目原样保留."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    comparisons: tuple[PlannedComparison, ...] = ()
    reason: Text
    deferred_work: tuple[Text, ...] = ()


class CandidatePlan(OwnerPlan):
    """候选目录的有限计划."""

    comparisons: tuple[PlannedCandidates, ...] = ()


class ManagedIntegrationSession(ControlledSession):
    """默认入口和冻结回放共用的主整合路径; 每个阶段及工作只执行一次."""

    def _build_context(
        self,
        *,
        extra: dict[str, dict[str, object]] | None = None,
        evidence_ids: list[str] | None = None,
        last_action: dict[str, object] | None = None,
    ) -> HumanMessage:
        if self.outline is None:
            return ExplorationSession._build_context(self)
        payload = getattr(self, "_aux_payload", None)
        if payload is None:
            return super()._build_context(extra=extra, evidence_ids=evidence_ids, last_action=last_action)
        blocks: list[str | dict[str, object]] = [
            {
                "type": "text",
                "text": json.dumps(
                    {"user_request": self.user_request, **payload, "submission": self._repair_context(), "budget": self._budget_context()},
                    ensure_ascii=False,
                ),
            }
        ]
        if self._phase_scope.startswith("outline-"):
            blocks.append({"type": "image_url", "image_url": {"url": self.reference_url}})
        return HumanMessage(content=blocks)

    def on_prepared(self, messages: list[BaseMessage]) -> None:
        """辅助阶段独立记录实际呈现, 不把发现目录授予库修改权."""
        super().on_prepared(messages)
        texts = [
            block.get("text") for message in messages if isinstance(message.content, list) for block in message.content if isinstance(block, dict)
        ]
        if getattr(self, "_aux_payload", None) is not None and self._context_text in texts:
            self._aux_presented = True
            if self._phase_scope.startswith("outline-"):
                self._require_store().present_materials(self._aux_versions)

    def _take_step(self, identity: str) -> bool:
        if self.options.max_main_calls and self.execution.model_calls >= self.options.max_main_calls:
            reason = "global_call_budget_exhausted"
        elif self.integration_cycles >= self.options.max_integration_packages:
            reason = "integration_stage_budget_exhausted"
        else:
            self.integration_cycles += 1
            return True
        self.gaps.append({"work_id": identity, "reason": reason})
        self.work_results.append({"work_id": identity, "status": "not_run", "reason": reason, "model_calls": 0})
        return False

    def _run_aux(
        self,
        model: BaseChatModel,
        identity: str,
        prompt: str,
        schema: type[BaseModel],
        name: str,
        payload: dict[str, object],
        submit: Callable[[dict[str, object]], str],
    ) -> bool:
        if not self._take_step(identity):
            return False
        self._work, self.submission_handler = None, None
        self._history_chars, self._history_messages, self._consumed_calls, self._last_request = 0, [], set(), None
        self._aux_payload, self._aux_presented, self._aux_done = payload, False, False
        self._aux_revision = self._require_store().revision
        self._aux_versions = self._require_store().material_versions(list(self._require_store().material_entries()))
        self._phase_scope = identity

        def guarded(arguments: dict[str, object]) -> str:
            if not self._aux_presented or self._require_store().revision != self._aux_revision:
                review_error("/", "Receive the current complete step materials before submitting")
            reply = submit(arguments)
            self._aux_done, self.last_action = True, json.loads(reply)
            return reply

        @tool(name, args_schema=schema)
        def submit_step(**arguments: object) -> str:
            """提交当前阶段的完整决定; 禁止超出本阶段字段."""
            return guarded(arguments)

        self.submission_handler = SubmissionHandler(
            f"{self.task_id}-{identity}",
            submit_step,
            guarded,
            lambda: json.dumps(self.last_action, ensure_ascii=False) if self._aux_done else None,
            self.lock,
            directory=self.directory,
        )
        start, reason, status = self.execution.model_calls, "", "submitted"
        try:
            self._run_step(
                model,
                prompt,
                [submit_step, self.submission_handler.repair_tool()],
                lambda: self._aux_done,
                scope=identity,
                max_calls=self.options.max_integration_calls,
            )
        except (AnalysisLimitError, AnalysisNoProgressError) as exc:
            reason, status = str(exc), "deferred"
            self.gaps.append({"work_id": identity, "reason": reason})
        except (Exception, KeyboardInterrupt) as exc:
            reason, status = f"{type(exc).__name__}: {exc}", "failed"
            raise
        finally:
            self.work_results.append({"work_id": identity, "status": status, "reason": reason, "model_calls": self.execution.model_calls - start})
            self._aux_payload = None
            self.save()
        return self._aux_done

    def _discovery_entries(self, *, candidates: bool) -> list[dict[str, object]]:
        entries = self._require_store().material_entries()
        if candidates:
            # 只省略无关元素和草图; 所有拥有者及候选都可参与发现, 不限已合并拥有者.
            return [entry for entry in entries.values() if entry["kind"] in {"feature", "relation", "candidate"}]
        result = []
        for entry in entries.values():
            if entry["kind"] not in {"feature", "relation", "sketch"}:
                continue
            content = cast("dict[str, object]", entry["content"])
            fields = ("id", "name", "element_ids", "appearance", "kind", "participants", "description", "composition")
            result.append({"handle": entry["handle"], "kind": entry["kind"], "content": {key: content[key] for key in fields if key in content}})
        return result

    def _discover(self, model: BaseChatModel, *, candidates: bool) -> list[ControlledWork]:
        identity = "discover-candidates" if candidates else "discover-owners"
        entries = self._discovery_entries(candidates=candidates)
        eligible = {str(entry["handle"]): entry for entry in entries if (entry["kind"] == "candidate") == candidates}
        if len(eligible) < MIN_COMPARISON_TARGETS:
            return []
        works: list[ControlledWork] = []
        schema = CandidatePlan if candidates else OwnerPlan

        def submit(arguments: dict[str, object]) -> str:
            plan = schema.model_validate(arguments)
            seen: set[str] = set()
            staged = []
            for index, item in enumerate(plan.comparisons):
                targets = set(item.target_ids)
                if len(targets) != len(item.target_ids) or seen.intersection(targets) or targets - eligible.keys():
                    review_error(f"/comparisons/{index}/target_ids", "Use distinct shown identities, each in at most one group")
                if len(targets) > self.options.max_comparison_targets or any(eligible[target]["kind"] != item.kind for target in targets):
                    review_error(f"/comparisons/{index}", "Use one allowed kind within the comparison target limit")
                if candidates and len({str(eligible[target]["owner"]) for target in targets}) != 1:
                    review_error(f"/comparisons/{index}/target_ids", "Candidates must have the same canonical owner")
                seen.update(targets)
                staged.append(ControlledWork(id=f"{identity}-{index + 1}", kind=item.kind, target_ids=item.target_ids, question=item.question))
            works.extend(staged)
            self.gaps.extend({"work_id": identity, "reason": reason} for reason in plan.deferred_work)
            return json.dumps(
                {
                    "status": "planned",
                    "work_ids": [work.id for work in works],
                    "unselected_ids": sorted(eligible.keys() - seen),
                    "reason": plan.reason,
                },
                ensure_ascii=False,
            )

        self._run_aux(
            model,
            identity,
            DISCOVERY_PROMPT,
            schema,
            "plan_comparisons",
            {
                "kind": identity,
                "entries": entries,
                "stages_remaining": self.options.max_integration_packages - self.integration_cycles,
                "owner_comparisons": self._owner_history() if candidates else [],
            },
            submit,
        )
        order = {"feature": 0, "relation": 1, "sketch": 2, "candidate": 3}
        return sorted(works, key=lambda work: order[work.kind])

    def _owner_history(self) -> list[dict[str, object]]:
        """向候选发现提供已有拥有者判断, 不重复发送呈现哈希和事务元数据."""
        store = self._require_store()
        result = []
        for work in self.work_results:
            if work.get("comparison_id"):
                targets = cast("list[str]", work["target_ids"])
                result.append(
                    {
                        "target_ids": targets,
                        "current_target_ids": list(dict.fromkeys(store.resolve(identity) for identity in targets)),
                        "status": work["status"],
                        "explanation": work.get("explanation", ""),
                        "reason": work["reason"],
                    }
                )
        return result

    def _review_outline(self, model: BaseChatModel) -> None:
        if not self.issues or self.outline is None:
            return
        previous = self.outline
        proposed: list[dict[str, object]] = []

        def submit(arguments: dict[str, object]) -> str:
            review = OutlineReview.model_validate(arguments)
            updated = revised_outline(previous, self.issues, review)
            if updated != previous:
                self._require_store().revise_outline(updated, {}, {})
                self.outline = updated
                self.outline_versions.append(updated)
            for item in review.decisions:
                proposed.append(item.model_dump())
                self.resolutions[item.issue_id] = {
                    **item.model_dump(),
                    "disposition": "deferred" if item.disposition == "deferred" else "verification_pending",
                    "proposed_disposition": item.disposition,
                    "outline_version": len(self.outline_versions),
                }
            self.save()
            return json.dumps({"status": "review_proposed", "outline_version": len(self.outline_versions)}, ensure_ascii=False)

        payload: dict[str, object] = {
            "kind": "outline-review",
            "visual_outline": asdict(previous),
            "issues": self.issues,
            "entries": list(self._require_store().material_entries().values()),
        }
        if not self._run_aux(model, "outline-review", ISSUE_PROMPT, OutlineReview, "review_outline", payload, submit):
            return
        expected = {str(item["issue_id"]) for item in proposed if item["disposition"] != "deferred"}
        if not expected:
            return

        def verify(arguments: dict[str, object]) -> str:
            decision = VerifyOutlineReview.model_validate(arguments)
            require_issue_ids([item.issue_id for item in decision.decisions], expected)
            for item in decision.decisions:
                proposal = self.resolutions[item.issue_id]
                self.resolutions[item.issue_id] = {
                    **proposal,
                    "disposition": proposal["proposed_disposition"] if item.confirmed else "deferred",
                    "verification_reason": item.reason,
                    "verified_outline_version": len(self.outline_versions),
                }
            self.save()
            return json.dumps({"status": "outline_review_verified"})

        self._run_aux(
            model,
            "outline-verify",
            VERIFY_PROMPT,
            VerifyOutlineReview,
            "verify_outline",
            {
                **payload,
                "kind": "outline-verify",
                "previous_outline": asdict(previous),
                "visual_outline": asdict(self.outline),
                "proposals": proposed,
                "verification_issue_ids": sorted(expected),
                "entries": list(self._require_store().material_entries().values()),
            },
            verify,
        )

    def _integrate(self, model: BaseChatModel) -> None:
        self._review_outline(model)
        for candidates in (False, True):
            for work in self._discover(model, candidates=candidates):
                if not self._take_step(work.id):
                    self.work_results[-1].update(target_ids=list(work.target_ids), question=work.question, kind=work.kind)
                    continue
                self._scheduled.add(work.id)
                self._execute_work(model, work, max_work_calls=self.options.max_integration_calls)
        self.finish_requested = True
        self.finalize()
        self.save()
