"""单个原生观察角色、请求级固定材料与执行准入."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRetryMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from shader_deep.agents.analysis.draft_backend import DraftBackend
from shader_deep.agents.draft_runtime import DraftToolGuard
from shader_deep.domain.evidence import ImageRegion, Text  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.domain.scene_observations import SceneObservationSubmission  # noqa: TC001  # 工具 schema 在运行时解析类型.
from shader_deep.infrastructure.llm.messages import _message
from shader_deep.infrastructure.llm.options import ResolvedModelOptions
from shader_deep.infrastructure.llm.transport import _retryable, configure_analysis_model, validate_analysis_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain.tools import BaseTool
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import Runnable

    from shader_deep.workflows.analysis.session import AnalysisSession
    from shader_deep.workflows.options import AnalysisOptions

ALLOWED_TOOLS = frozenset({"read_file", "edit_file", "save_observation_draft", "finish_analysis", "stop_analysis", "crop", "region_stats"})
TERMINAL_TOOLS = frozenset({"finish_analysis", "stop_analysis"})
WRITE_TOOLS = TERMINAL_TOOLS | {"save_observation_draft", "edit_file"}
SKILL_PATH = "/skills/scene-observer/"
SKILL_FILES = ("SKILL.md", "references/grouping.md", "references/relations.md", "references/uncertainty.md")


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _skill_texts() -> dict[str, str]:
    resource = files("shader_deep.resources").joinpath("skills", "scene-observer")
    return {name: resource.joinpath(name).read_text(encoding="utf-8") for name in SKILL_FILES}


def _resources() -> dict[str, object]:
    return {SKILL_PATH + name: create_file_data(text) for name, text in _skill_texts().items()}


def analysis_input(session: AnalysisSession) -> dict[str, object]:
    """预置可读资源; 原图只由请求级装配注入, 避免首轮重复."""
    return {"messages": [HumanMessage(content=session.request)], "files": _resources()}


def _tools(session: AnalysisSession) -> list[BaseTool]:
    @tool
    def save_observation_draft(report: SceneObservationSubmission) -> str:
        """首次保存观察草稿并返回诊断, 之后回读并局部编辑."""
        return _encoded(session.save_draft(report))

    @tool
    def finish_analysis() -> str:
        """从已回读的最新草稿冻结并完成观察, 不接收全文参数."""
        return _encoded(session.finish_draft())

    @tool
    def stop_analysis(reason: str) -> str:
        """无法提交可靠观察时以非空原因结束, 不登记成功报告."""
        return _encoded(session.stop(reason))

    @tool
    def crop(region: ImageRegion, question: Text) -> str:
        """裁剪固定原图中以像素表示的左闭右开区域, 裁图随下一次请求提供."""
        return _encoded(session.measure_reference("crop", region, question))

    @tool
    def region_stats(region: ImageRegion, question: Text) -> str:
        """测量原图区域在白底合成后的编码 RGB 与亮度均值, 可对照相邻区域."""
        return _encoded(session.measure_reference("region_stats", region, question))

    return [save_observation_draft, finish_analysis, stop_analysis, crop, region_stats]


class ObservationGuard(DraftToolGuard):
    """将共用草稿执行保护绑定到观察角色的受控路径."""

    def __init__(self, session: AnalysisSession) -> None:
        """固定本角色工具与可读文件范围."""
        super().__init__(
            session,
            allowed_tools=ALLOWED_TOOLS,
            write_tools=WRITE_TOOLS,
            draft_path="/draft/observations.json",
            resource_paths=frozenset(SKILL_PATH + name for name in SKILL_FILES),
            readable_prefixes=("/conversation_history/", "/large_tool_results/"),
        )


class FixedInput(AgentMiddleware[AgentState[object], None]):
    """只装配实际请求, 不向 SDK 持久历史重复追加原图."""

    def __init__(self, session: AnalysisSession, *, transport_guard: bool) -> None:
        """绑定固定材料及服务响应校验开关."""
        self.session, self.transport_guard = session, transport_guard

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        self.session.bind_request()
        payload: dict[str, object] = dict(self.session.context())
        fixed = _message(payload, self.session.reference_url)
        if isinstance(fixed.content, list):
            for identity, url in self.session.crop_images():
                fixed.content.extend(
                    [{"type": "text", "text": _encoded({"reference_crop_id": identity})}, {"type": "image_url", "image_url": {"url": url}}]
                )
        messages = [*request.messages, fixed]
        tools = [entry for entry in request.tools if getattr(entry, "name", None) in ALLOWED_TOOLS]
        return request.override(messages=messages, tools=tools)

    def _validated(self, response: ModelResponse) -> ModelResponse:
        if not self.transport_guard:
            return response
        return replace(response, result=[validate_analysis_message(item) if isinstance(item, AIMessage) else item for item in response.result])

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        """保持每次实际请求中原图与业务上下文可见."""
        prepared = self._prepare(request)
        result = self._validated(handler(prepared))
        self.session.present_reads(list(prepared.messages))
        return result

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelResponse:
        """异步请求同样注入实际图像内容."""
        prepared = self._prepare(request)
        result = self._validated(await handler(prepared))
        self.session.present_reads(list(prepared.messages))
        return result


def build_analysis_agent(model: BaseChatModel, session: AnalysisSession, options: AnalysisOptions) -> Runnable:
    """装配单个 SDK 观察图, 原图与角色能力由程序固定.

    Args:
        model: 已构造的模型, 测试可注入无网络实现.
        session: 本次观察会话.
        options: 通用传输配置与有限模型调用预算.

    Returns:
        由 SDK 管理消息、文件、摘要和工具循环的原生图.
    """
    resources = _skill_texts()
    hashes = {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in resources.items()}
    identifier = getattr(model, "model_name", None)
    session.record_runtime(
        {
            "model_id": identifier if isinstance(identifier, str) else None,
            "rule_sha256": hashes["SKILL.md"],
            "resource_sha256": hashes,
            "skill_bundle_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest(),
        }
    )
    selected = (
        configure_analysis_model(
            model, ResolvedModelOptions(options.request_timeout_seconds, options.stream_model_responses, options.max_output_tokens)
        )
        if isinstance(model, ChatOpenAI)
        else model
    )
    return create_deep_agent(
        model=selected,
        tools=_tools(session),
        name="scene-observer",
        system_prompt=resources["SKILL.md"],
        backend=CompositeBackend(default=StateBackend(), routes={"/draft/": DraftBackend(session)}),
        # 专用角色已激活主流程, 条件式参考通过 read_file 披露; 不再提示重复发现同一 skill.
        subagents=[],
        middleware=[
            ObservationGuard(session),
            ModelRetryMiddleware(max_retries=options.max_request_retries, retry_on=_retryable, on_failure="error"),
            FixedInput(session, transport_guard=isinstance(model, ChatOpenAI)),
        ],
    )
