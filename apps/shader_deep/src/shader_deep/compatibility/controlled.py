"""按固定范围执行局部比较, 供默认整合和冻结实验共用."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict

from shader_deep.compatibility.ondemand.session import ExplorationSession, StaleMaterialError
from shader_deep.domain.errors import AnalysisValidationError
from shader_deep.domain.library.decisions import IntegrationDecision, MergeDecision
from shader_deep.domain.primitives import Text  # noqa: TC001  # Pydantic 在运行时解析工具字段.
from shader_deep.infrastructure.llm.transport import configure_analysis_model
from shader_deep.runtime.execution import AnalysisLimitError, AnalysisNoProgressError
from shader_deep.runtime.runner import AnalysisLoop
from shader_deep.runtime.submissions.handler import SubmissionHandler
from shader_deep.workflows.configuration import resolve_phase_options

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_openai import ChatOpenAI
    from pydantic import JsonValue

    from shader_deep.domain.library.materials import ComparisonGroup
    from shader_deep.domain.tasks import BlackboardState
    from shader_deep.workflows.options import AnalysisOptions

MIN_DEPENDENCY_OWNERS = 2

CONTROLLED_PROMPT = """你负责程序固定范围的一次去重比较。只根据当前完整文本材料判断, 不猜测缺失证据。
先在回复正文简述合并或保留的依据, 再调用 submit_integration。按当前工作类型的判断标准比较。
对象 ID 和候选 ID 仅作引用, 不从名称推断未呈现的正文或机制。merges 只能修改当前目标的同类型对象; preserve=true 明确保留
所有未合并目标。deferred_work 给出具体对象、差异和缺失证据, 暂缓整组, 不能同时提交合并或 preserve。
所有目标必须被合并、明确保留或整组暂缓。当前工具不允许增补、取证、元素未决问题处置或任意引用更新。
拒绝回执可在本工作额度内用 repair_analysis_submission 局部修复; 不要管理工作身份或扩大目标。
"""

FEATURE_PROMPT = """当前是特征比较, 判断同一对象范围中的外观描述是否等价。
比较 element_ids、appearance 中的区域、方向、形状、强弱和边界等限定; 名称相似不足以证明等价。
对象范围一致、外观描述等价且没有互相冲突的限定时可以合并; 范围不同或外观有实质差异时保留,
现有外观材料确实无法判断时才暂缓并说明缺少的外观证据。
特征是可由不同机制解释的外观条目。合并特征会完整保留双方所有候选及来源, 不合并候选,
也不认定它们的机制相同。候选列表不同或候选机制正文未提供, 本身不是暂缓特征比较的理由;
本工作无需选择真实实现机制, 候选机制的比较由独立候选工作完成。
"""

CANDIDATE_PROMPT = """当前是候选比较, 判断形成机制及适用前提是否等价。
根据完整候选 mechanism、reasoning、requires 及规范拥有者正文比较, 只有同一规范拥有者下
机制等价且必要前提一致才可以合并。不能因为结果外观类似就吞并不同机制;
拥有者特征已经合并不表示其候选机制等价。明确保留不同机制或不同前提的候选,
当前材料不足以判断时说明具体缺口并暂缓整组。
"""

RELATION_PROMPT = """当前比较关系。只有关系类型、参与端点、实例范围和方向一致, 描述同一组织现象时才合并。
不同方向或适用条件必须保留。关系合并保留全部候选, 不要求其机制相同, 不从候选 ID 推断机制。
"""

SKETCH_PROMPT = """当前比较组成草图。根据完整组成和已有引用判断是否描述同一组成。
不同结构或组合保留。后端机械维护引用并校验整笔事务; 引用冲突不能通过猜测缺失正文或扩大权限解决,
当前范围无法合法发布时暂缓整组并说明具体冲突。
"""

ComparisonKind = Literal["feature", "relation", "sketch", "candidate"]


@dataclass(frozen=True)
class ControlledWork:
    """人工指定的局部判断; 依赖只要求已有拥有者完成归并."""

    id: str
    kind: ComparisonKind
    target_ids: tuple[str, ...]
    question: str
    depends_on: str | None = None
    owner_ids: tuple[str, ...] = ()


class FeatureMerge(MergeDecision):
    """受控特征步骤不能请求其他类型的合并."""

    kind: Literal["feature"]


class CandidateMerge(MergeDecision):
    """受控候选步骤不能请求其他类型的合并."""

    kind: Literal["candidate"]


class RelationMerge(MergeDecision):
    """关系比较只允许关系合并."""

    kind: Literal["relation"]


class SketchMerge(MergeDecision):
    """草图比较只允许草图合并."""

    kind: Literal["sketch"]


class FeatureDecision(BaseModel):
    """仅保留现有整合协议在特征比较中允许的字段."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    merges: tuple[FeatureMerge, ...] = ()
    preserve: bool = False
    deferred_work: tuple[Text, ...] = ()


class CandidateDecision(BaseModel):
    """仅保留现有整合协议在候选比较中允许的字段."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    merges: tuple[CandidateMerge, ...] = ()
    preserve: bool = False
    deferred_work: tuple[Text, ...] = ()


class RelationDecision(FeatureDecision):
    """关系比较的受限决定."""

    merges: tuple[RelationMerge, ...] = ()


class SketchDecision(FeatureDecision):
    """组成比较的受限决定."""

    merges: tuple[SketchMerge, ...] = ()


class ControlledSession(ExplorationSession):
    """固定清单每项只运行一次, 待处理台账不会触发自动重入."""

    def __init__(self, state: BlackboardState, task_id: str, options: AnalysisOptions, directory: Path, reference_url: str) -> None:
        """复用原会话输入, 附加当前试验结果及已安排身份.

        Args:
            state: 已冻结探索输入的黑板.
            task_id: 原始主任务身份.
            options: 当前试验预算.
            directory: 本次独立运行目录.
            reference_url: 用于验证来源的原图, 不发送给模型.
        """
        super().__init__(state, task_id, options, directory, reference_url)
        self.work_results: list[dict[str, object]] = []
        self._scheduled: set[str] = set()
        self._work: ControlledWork | None = None
        self._phase_limit = 0
        self._phase_scope = "outline"
        self._work_explanation = ""

    def remaining_calls(self) -> int | None:
        """返回全局剩余额度; 独立角色可扣除派发前已消耗的调用."""
        return max(0, self.options.max_main_calls - self.execution.model_calls) if self.options.max_main_calls else None

    def _budget_context(self) -> dict[str, int | None]:
        return {
            "work_calls_remaining": max(0, self._phase_limit - self.execution.model_calls),
            "total_calls_remaining": self.remaining_calls(),
        }

    def _build_context(
        self,
        *,
        extra: dict[str, dict[str, object]] | None = None,
        evidence_ids: list[str] | None = None,
        last_action: dict[str, object] | None = None,
    ) -> HumanMessage:
        if self.outline is None:
            return super()._build_context(extra=extra, evidence_ids=evidence_ids, last_action=last_action)
        if extra or evidence_ids:
            msg = "Controlled comparison cannot extend materials or request measurements"
            raise ValueError(msg)
        group = self.comparisons.active
        payload = {
            "comparison": group.business_payload() if group else None,
            "submission": self._repair_context(),
            "last_action": self.last_action if last_action is None else last_action,
            "budget": self._budget_context(),
        }
        return HumanMessage(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    def context(self) -> HumanMessage:
        """纯文本呈现完整当前组, 不重复无关初稿、整图或目录."""
        if self.outline is None:
            return super().context()
        message = self._build_context()
        self._context_text = str(cast("list[dict[str, object]]", message.content)[0]["text"])
        group = self.comparisons.active
        self._context_versions = dict(group.versions) if group else {}
        self._context_issue_ids = set()
        return message

    def integration_tools(self) -> list[BaseTool]:
        """模型 schema 和执行白名单同时限制在当前比较及其局部修复."""
        schema = self._decision_schema()

        @tool(args_schema=schema)
        def submit_integration(**arguments: object) -> str:
            """提交当前目标的合并、明确保留或整组暂缓; 不允许其他字段."""
            return self._submit_integration(arguments)

        self.submission_handler = SubmissionHandler(
            f"{self.task_id}-controlled-{self._work.id if self._work else 'unknown'}",
            submit_integration,
            self._submit_integration,
            lambda: json.dumps(self.last_action, ensure_ascii=False) if self.group_accepted else None,
            self.lock,
            directory=self.directory,
        )
        return [submit_integration, self.submission_handler.repair_tool()]

    def _submit_integration(self, arguments: dict[str, object]) -> str:
        schema = self._decision_schema()
        decision = schema.model_validate(arguments)
        if decision.deferred_work and (decision.merges or decision.preserve):
            raise AnalysisValidationError(
                [{"path": "/deferred_work", "code": "whole_work_deferral", "message": "Defer the whole work without merges or preserve"}]
            )
        return super()._submit_integration(IntegrationDecision.model_validate(decision.model_dump()).model_dump())

    def _decision_schema(self) -> type[FeatureDecision | CandidateDecision | RelationDecision | SketchDecision]:
        if self._work is None:
            msg = "Start a controlled work before submitting its decision"
            raise ValueError(msg)
        return {"feature": FeatureDecision, "candidate": CandidateDecision, "relation": RelationDecision, "sketch": SketchDecision}[self._work.kind]

    def _dependency_reason(self, work: ControlledWork) -> str | None:
        if work.depends_on is None:
            return None
        prior = next((result for result in self.work_results if result["work_id"] == work.depends_on), None)
        if prior is None or prior["status"] != "modified":
            return "dependency_not_merged: " + work.depends_on
        owners = {self._require_store().resolve(identity) for identity in work.owner_ids}
        if len(work.owner_ids) < MIN_DEPENDENCY_OWNERS or len(owners) != 1:
            return "dependency_owners_not_merged: " + work.depends_on
        return None

    def _start_work(self, work: ControlledWork) -> str:
        store = self._require_store()
        targets = list(dict.fromkeys(store.resolve(identity) for identity in work.target_ids))
        entries = store.material_entries()
        if not targets or any(identity not in entries or entries[identity]["kind"] != work.kind for identity in targets):
            msg = "Controlled work requires existing targets of its declared kind"
            raise ValueError(msg)
        if len(targets) > self.options.max_comparison_targets:
            msg = "Controlled work exceeds comparison target limit"
            raise ValueError(msg)
        self._work = work
        self._work_explanation = ""
        group = self.comparisons.start(store, work.question, targets)
        required = store.comparison_materials(targets)
        self.comparisons.append(
            {identity: entries[identity] for identity in required}, store.material_versions(required), library_revision=store.revision
        )
        self.group_accepted, self._accepted_arguments = False, None
        self.submission_handler = None
        self.last_action = {"work_id": work.id, "status": "compare_current_targets"}
        self._history_chars, self._history_messages, self._consumed_calls = 0, [], set()
        self._last_request = None
        return group.id

    def _run_work_phase(self, model: BaseChatModel, *, max_work_calls: int) -> None:
        tools = self.integration_tools()
        scope = self.comparisons.active.id if self.comparisons.active else "controlled"
        self._run_step(model, self._work_prompt(), tools, lambda: self.group_accepted, scope=scope, max_calls=max_work_calls)

    def _run_step(self, model: BaseChatModel, prompt: str, tools: list[BaseTool], done: Callable[[], bool], *, scope: str, max_calls: int) -> None:
        """所有整合步骤共用有限额度、请求准入和按身份累计的修复记录."""
        self.phase_options, source = resolve_phase_options(self.options, "integration")
        self.phase_budgets["integration"] = {"max_output_tokens": self.phase_options.max_output_tokens, "source": source}
        self._tools, self._phase_scope = tools, scope
        self._phase_limit = self.execution.model_calls + max_calls
        remaining = self.remaining_calls()
        if remaining is not None:
            self._phase_limit = min(self._phase_limit, self.execution.model_calls + remaining)
        loop = AnalysisLoop(
            self.execution,
            self._phase_limit,
            self.context,
            done,
            self._tools,
            request_retries=self.options.max_request_retries,
            submission_handler=self.submission_handler,
            on_prepared=self.on_prepared,
            before_request=self._check_request,
            role_prompt_only=True,
            available_tools=self._active_tools,
            on_history=self._record_history,
            prepare_history=self._prepare_history,
            repair_scope=lambda: scope,
            repair_scope_active=lambda active: active == scope,
            on_tool_result=self._save_tool_result,
            on_event=lambda event, details: self.event(
                "phase_checkpoint" if event == "task_completed" else event,
                {"phase": "integration", "work_id": self._work.id if self._work else scope, **details},
            ),
            max_repeated_no_progress=self.options.max_repeated_no_progress or self.options.integration_no_progress,
            progress=lambda: self._require_store().revision,
        )
        self.save()
        loop.run(configure_analysis_model(cast("ChatOpenAI", model), self.phase_options), prompt, self.config(self.task_id))

    def _work_prompt(self) -> str:
        """类型由程序固定, 避免把候选机制标准用于特征外观比较."""
        if self._work is None:
            msg = "Start a controlled work before selecting its comparison criteria"
            raise ValueError(msg)
        criteria = {"feature": FEATURE_PROMPT, "candidate": CANDIDATE_PROMPT, "relation": RELATION_PROMPT, "sketch": SKETCH_PROMPT}[self._work.kind]
        return CONTROLLED_PROMPT + "\n" + criteria

    def _record_history(self, messages: list[BaseMessage]) -> None:
        super()._record_history(messages)
        for message in messages:
            if isinstance(message, AIMessage):
                if self._work is not None and message.content:
                    self._work_explanation = message.text
                self.event("controlled_explanation", {"work_id": self._work.id if self._work else None, "content": message.content})

    def _save_tool_result(self, record: dict[str, JsonValue]) -> None:
        super()._save_tool_result({**record, "work_id": self._work.id if self._work else self._phase_scope})

    def save(self) -> None:
        """保存主快照及工作结果, 异常退出也保留已消耗额度和比较回执."""
        super().save()
        destination = self.directory / "integration-work.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.work_results, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _execute_work(self, model: BaseChatModel, work: ControlledWork, *, max_work_calls: int) -> None:
        start = self.execution.model_calls
        self._start_work(work)
        group = cast("ComparisonGroup", self.comparisons.active)
        reason, status = "", ""
        try:
            self._run_work_phase(model, max_work_calls=max_work_calls)
        except (AnalysisLimitError, AnalysisNoProgressError, StaleMaterialError) as exc:
            reason = str(exc)
            if self.comparisons.active is not None:
                self.comparisons.defer(reason)
        except (Exception, KeyboardInterrupt) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            if self.comparisons.active is not None:
                self.comparisons.defer(reason)
            raise
        finally:
            # 服务异常与中断同样保留已发送材料和真实调用数, 然后继续向上抛出原异常.
            self._record_work_result(work, group, start, status=status, reason=reason)

    def _record_work_result(
        self,
        work: ControlledWork,
        group: ComparisonGroup,
        start: int,
        *,
        status: str,
        reason: str,
    ) -> None:
        decision = self._accepted_arguments or {}
        if not status:
            status = "deferred" if not self.group_accepted or decision.get("deferred_work") else "modified" if decision.get("merges") else "preserved"
        self.work_results.append(
            {
                "work_id": work.id,
                "comparison_id": group.id,
                "status": status,
                "reason": reason or "; ".join(cast("list[str]", decision.get("deferred_work", []))),
                "explanation": self._work_explanation,
                "model_calls": self.execution.model_calls - start,
                "target_ids": list(group.target_ids),
                "presented_versions": dict(group.presented_versions),
                "receipt": dict(self.last_action) if self.group_accepted else None,
            }
        )
        self.save()

    def run_controlled(self, model: BaseChatModel, works: tuple[ControlledWork, ...], *, max_work_calls: int) -> None:
        """顺序执行一次固定清单, 局部失败继续独立项, 全局额度耗尽停止.

        Args:
            model: 原有配置构造的模型客户端.
            works: 由程序固定的比较及前置依赖.
            max_work_calls: 每项包含修复在内的有限调用上限.

        Raises:
            ValueError: 全局或单项预算无界, 工作身份重复, 或试图重新执行已安排项.
        """
        identities = [work.id for work in works]
        if (
            self.stop_reason != "running"
            or self.options.max_main_calls <= 0
            or max_work_calls <= 0
            or len(set(identities)) != len(identities)
            or self._scheduled.intersection(identities)
        ):
            msg = "Controlled replay requires an open session, positive budgets and unique, never scheduled work IDs"
            raise ValueError(msg)
        for work in works:
            self._scheduled.add(work.id)
            reason = self._dependency_reason(work)
            if self.execution.model_calls >= self.options.max_main_calls:
                reason = "global_call_budget_exhausted"
            if reason:
                self.work_results.append(
                    {
                        "work_id": work.id,
                        "comparison_id": None,
                        "status": "not_run",
                        "reason": reason,
                        "model_calls": 0,
                        "target_ids": list(work.target_ids),
                        "receipt": None,
                        "presented_versions": {},
                    }
                )
                self.gaps.append({"work_id": work.id, "reason": reason})
                continue
            self._execute_work(model, work, max_work_calls=max_work_calls)
        self.finish_requested = True
        self.finalize()
        self.save()
