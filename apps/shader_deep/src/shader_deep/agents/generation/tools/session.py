"""工具绑定、运行快照和串行浏览器会话的生命周期."""

# RenderSession 是一次生成运行的工作台, 由 run_generation 的 with 语句管理.
# 生命周期: 创建会话 -> 启动单工作线程 -> 按需启动浏览器 -> 多次工具调用 -> 关闭资源.

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Literal

from langchain.tools import BaseTool, tool
from langchain_core.tools import InjectedToolCallId
from pydantic import Field, StrictInt

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.agents.generation.memory import GenerationMemory, ReadMaterialRequest, WorkflowRecall
from shader_deep.agents.generation.tools.finish import select_candidate, stop_generation
from shader_deep.agents.generation.tools.optimize import optimize_candidate
from shader_deep.agents.generation.tools.render import render_candidate
from shader_deep.domain.shader_parameters import ParameterSpec  # noqa: TC001  # 工具参数在运行时生成 schema.
from shader_deep.domain.visual_review import PixelRegion  # noqa: TC001  # 工具参数在运行时生成 schema.
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run
from shader_deep.infrastructure.storage.generation_planning_inputs import CapturedPlanningInputs
from shader_deep.infrastructure.storage.scene_inputs import CapturedSceneInputs, scene_input_records
from shader_deep.optimization import DEFAULT_OPTIMIZATION_EVALUATIONS, MAX_OPTIMIZATION_EVALUATIONS, ProtectedRegion

if TYPE_CHECKING:
    # 这些类型仅供检查; 不在运行时互相导入, 避免 session 与执行模块形成循环导入.
    from pathlib import Path
    from types import TracebackType
    from typing import Self

    from shader_deep.agents.generation.options import GenerationOptions
    from shader_deep.domain.local_probe import LocalProbeBinding
    from shader_deep.domain.tasks import BlackboardState, CandidateRecord
    from shader_deep.infrastructure.storage.generation_inputs import CapturedGenerationInputs
    from shader_deep.rendering import WebGL2Renderer


class RenderSession:
    """一个生成任务的工具状态与浏览器生命周期."""

    def __init__(
        self,
        state: BlackboardState,
        task_id: str,
        options: GenerationOptions,
        asset_root: Path,
        *,
        run_dir: Path | None = None,
        inputs: CapturedGenerationInputs | CapturedSceneInputs | CapturedPlanningInputs | None = None,
    ) -> None:
        """绑定黑板和执行条件.

        Args:
            state: 本次任务的起始黑板.
            task_id: 生成任务标识.
            options: 固定渲染条件与预算.
            asset_root: 输入制品根目录, 写入运行记录供追溯.
            run_dir: 准备阶段已分配的运行目录; 未提供时创建新目录.
            inputs: 单方案或整图任务已固定的材料, 供逐轮请求复用.
        """
        self.state = state
        self.task_id = task_id
        self.options = options
        self.asset_root = asset_root
        self.run_dir = run_dir if run_dir is not None else create_run_directory(options.output_dir)
        self.inputs = inputs
        self.inherited_limitations = inputs.limitations if isinstance(inputs, CapturedPlanningInputs) else ()
        # 尝试次数限制渲染成本, 模型轮数限制持续对话; 两者独立累计.
        self.attempts = 0
        self.model_calls = 0
        self.token_usage: dict[str, int] | None = None
        self.stop_reason = "running"
        self.error: str | None = None
        self.selected: CandidateRecord | None = None
        self.baked_code_path: Path | None = None
        self.reference_png: bytes | None = None
        self.reference_copy: Path | None = None
        self.reference_anchors: tuple[PixelRegion, ...] = ()
        # successful: 生成了 PNG; presented: 已将该 PNG 加入后续模型请求.
        # finish 要求同时满足两者, 防止模型在尚未收到预览时直接结束.
        self.presented: set[str] = set()
        self.successful: set[str] = set()
        self._worker: ThreadPoolExecutor | None = None
        self.renderer: WebGL2Renderer | None = None
        self.memory: GenerationMemory | None = None
        self.vista = WorkflowRecall(options.vista_root) if options.vista_root is not None else None

    @property
    def is_finished(self) -> bool:
        """是否已经完成、受阻或因预算和异常结束."""
        return self.stop_reason != "running"

    def __enter__(self) -> Self:
        """启动串行执行线程.

        Returns:
            已就绪的工具会话.
        """
        self.save()
        # 初始化处于工作流的异常保存范围内, 失败也记录运行目录和诊断.
        if self.options.visual_memory:
            if isinstance(self.inputs, (CapturedSceneInputs, CapturedPlanningInputs)):
                msg = "Visual memory currently supports PNG tasks and five_libraries_v1 reports only"
                raise ValueError(msg)
            self.memory = GenerationMemory(self.state, self.task_id, self.run_dir, self.asset_root, self.inputs)
        if self.options.optimize_parameters:
            self._pin_reference()
        # Playwright 同步对象需要在所属线程使用和关闭.
        # max_workers=1 同时保证多次工具调用串行, 不并发修改本会话的尝试计数.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shader-render")
        return self

    def _pin_reference(self) -> None:
        target = self.state["targets"][self.state["tasks"][self.task_id].target_version]
        if self.memory is not None:
            self.reference_png = self.memory.store.read(self.memory.reference).data
        else:
            self.reference_png = (self.asset_root / target.reference_path).read_bytes()
        if isinstance(self.inputs, CapturedPlanningInputs):
            self.reference_anchors = self.inputs.reference_anchors
        if self.inputs is None:
            self.reference_copy = self.run_dir / "inputs" / "reference.png"
            self.reference_copy.parent.mkdir(parents=True, exist_ok=True)
            self.reference_copy.write_bytes(self.reference_png)

    def _input_details(self) -> dict[str, object]:
        if isinstance(self.inputs, CapturedPlanningInputs):
            return {"inputs": {"planning": self.inputs.metadata()}}
        if isinstance(self.inputs, CapturedSceneInputs):
            return {"scene_plan": asdict(self.inputs.plan), "inputs": {"scene_elements": scene_input_records(self.inputs)}}
        baseline = self.inputs.baseline if self.inputs is not None else None
        reference = str(self.reference_copy) if self.reference_copy is not None else None
        return {"inputs": {"baseline": baseline.paths() if baseline is not None else None, "reference": reference}}

    def save(self) -> None:
        """保存本轮条件、预算、选择状态及黑板."""
        # 只保存材料路径与组合计划, GLSL/data URL 继续由捕获文件和请求内存承载.
        save_run(
            self.run_dir,
            self.state,
            {
                "task_id": self.task_id,
                "asset_root": str(self.asset_root),
                "phase": "generation",
                **self._input_details(),
                "render": {"width": self.options.width, "height": self.options.height, "time": self.options.time},
                "max_attempts": self.options.max_attempts,
                "max_model_calls": self.options.resolved_max_model_calls,
                "visual_memory": self.options.visual_memory,
                "optimize_parameters": self.options.optimize_parameters,
                "attempts": self.attempts,
                "model_calls": self.model_calls,
                "token_usage": self.token_usage,
                "stop_reason": self.stop_reason,
                "error": self.error,
                "selected_candidate_id": self.selected.id if self.selected is not None else None,
                "baked_code_path": str(self.baked_code_path) if self.baked_code_path is not None else None,
            },
        )

    def tools(self) -> list[BaseTool]:
        """创建供本轮模型调用的渲染、选择和受阻结束工具.

        Returns:
            渲染工具及两种明确结束动作.
        """
        # tool(name)(method) 把绑定方法转为模型 Tool:
        # name 来自显式名称, description 来自方法 docstring, 参数 schema 来自类型注解.
        # self 已绑定当前会话, 不会成为模型需要填写的参数.
        # 未启用 parse_docstring, Args 说明保留在整体描述中, 不拆成逐字段 description.
        tools = [tool("render_shader")(self.render_shader), tool("finish_shader")(self.finish_shader), tool("stop_generation")(self.stop_generation)]
        if self.options.optimize_parameters:
            tools.append(tool("optimize_uniforms")(self.optimize_uniforms))
        if self.memory is not None:
            tools.extend([tool("list_materials")(self.list_materials), tool("read_materials")(self.read_materials)])
        if self.vista is not None:
            tools.extend([tool("list_records")(self.list_records), tool("read_record")(self.read_record)])
        return tools

    def list_records(self) -> str:
        """列出工作流归档的参考图和候选, 返回可回读的 record 文件名."""
        if self.is_finished:
            return json.dumps({"status": "already_finished"})
        if self.vista is None:
            msg = "Workflow recall is not enabled"
            raise RuntimeError(msg)
        return self.vista.list_records()

    def read_record(self, record: str, content: Literal["image", "code", "both"] = "image") -> str:
        """按 record 回看图、代码或两者, 所选内容随下一次模型调用展开."""
        if self.is_finished:
            return json.dumps({"status": "already_finished"})
        if self.vista is None:
            msg = "Workflow recall is not enabled"
            raise RuntimeError(msg)
        return self.vista.read_record(record, content)

    def list_materials(self, kind: Literal["image", "code", "text", "json"] | None = None, limit: int = 20, cursor: str | None = None) -> str:
        """列出当前任务材料的简短目录, 已知 ID 可直接读取.

        Args:
            kind: 可选内容类别, 省略时列出所有类别.
            limit: 每页条数, 范围 1 到 50.
            cursor: 上次返回的下一页游标.
        """
        if self.is_finished:
            return json.dumps({"status": "already_finished"})
        if self._worker is None or self.memory is None:
            msg = "Visual memory session is not open"
            raise RuntimeError(msg)
        try:
            result = self._worker.submit(self.memory.store.list_materials, kind=kind, limit=limit, cursor=cursor).result()
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)

    def read_materials(self, items: list[ReadMaterialRequest], tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        """按 ID 回看实际材料, 图片可指定像素区域, 多项按顺序对照.

        Args:
            items: 材料 ID 与可选 region(x,y,width,height), 坐标相对于来源图左上角.
            tool_call_id: 框架注入的调用身份, 模型不填写.

        Returns:
            逐项读取状态, 实际图像或正文随下一次模型请求展示一轮.
        """
        if self.is_finished:
            return json.dumps({"status": "already_finished"})
        if self._worker is None or self.memory is None:
            msg = "Visual memory session is not open"
            raise RuntimeError(msg)
        return self._worker.submit(self.memory.read_materials, items, tool_call_id).result()

    def begin_attempt(self) -> str | None:
        """在串行线程检查终态和预算, 失败的尝试也计数."""
        if self.is_finished:
            return json.dumps(
                {"status": "already_finished", "stop_reason": self.stop_reason, "candidate_id": self.selected.id if self.selected else None}
            )
        if self.attempts >= self.options.max_attempts:
            self.save()
            return json.dumps({"status": "attempt_limit", "message": "渲染尝试预算已用完"})
        self.attempts += 1
        return None

    @property
    def local_probe(self) -> LocalProbeBinding | None:
        """返回本轮固定的局部试验条件."""
        return self.inputs.local_probe if isinstance(self.inputs, CapturedPlanningInputs) else None

    def render_shader(self, glsl_code: str, *, parameter_specs: list[ParameterSpec] | None = None) -> str:
        """编译并渲染完整 mainImage 代码, 下一轮将提供真实预览或编译错误.

        Args:
            glsl_code: 完整 ShaderToy 代码, 不声明版本、iResolution、iTime 或 main.
            parameter_specs: 参数模式可为连续量声明同名 GLSL uniform 与类型、默认值、上下界; 无参代码省略.

        Returns:
            候选 ID、执行状态和剩余尝试次数, 成功预览由 Context Builder 加载.
        """
        if self._worker is None:
            msg = "Render session is not open"
            raise RuntimeError(msg)
        return self._worker.submit(render_candidate, self, glsl_code, tuple(parameter_specs or ())).result()

    def optimize_uniforms(
        self,
        baseline_id: str,
        parameter_names: list[str] | None = None,
        regions: list[PixelRegion] | None = None,
        anchors: list[PixelRegion] | None = None,
        protected_regions: list[ProtectedRegion] | None = None,
        *,
        max_evaluations: Annotated[StrictInt, Field(ge=1, le=MAX_OPTIMIZATION_EVALUATIONS)] = DEFAULT_OPTIMIZATION_EVALUATIONS,
    ) -> str:
        """锁定本轮参数化候选的 GLSL 后搜索连续参数, 胜出预览需回看再选择.

        Args:
            baseline_id: 本轮成功渲染的参数化候选 ID.
            parameter_names: 本次允许调整的参数名; 省略表示开放声明内参数.
            regions: 目标区矩形, 参考画布左上坐标 x0,y0,x1,y1; 省略使用全图.
            anchors: 统计采样区; 省略沿用观察测量锚点, 空列表关闭继承.
            protected_regions: 以基线像素为准保留的已达标区域及归一化误差容差.
            max_evaluations: 本次像素评估预算, 包括起始评估, 可在范围内增加耦合参数的搜索次数.

        Returns:
            优化诊断, 有改善时附新候选与真实预览.
        """
        if self._worker is None:
            msg = "Render session is not open"
            raise RuntimeError(msg)
        return self._worker.submit(
            optimize_candidate,
            self,
            baseline_id,
            tuple(parameter_names) if parameter_names is not None else None,
            tuple(regions or ()),
            tuple(anchors) if anchors is not None else None,
            tuple(protected_regions or ()),
            max_evaluations=max_evaluations,
        ).result()

    def finish_shader(self, candidate_id: str, assessment: str) -> str:
        """选择本轮已渲染且已经收到预览的候选, 说明与参考的差异后结束.

        Args:
            candidate_id: render_shader 返回并已在后续模型调用中展示预览的候选 ID.
            assessment: 对照参考图的自检结论和仍然存在的差异, 不表示用户已验收.

        Returns:
            选择成功的确认或不可选择的具体原因.
        """
        if self._worker is None:
            msg = "Render session is not open"
            raise RuntimeError(msg)
        # 选择也进入同一队列, 与渲染的状态更新保持串行.
        return self._worker.submit(select_candidate, self, candidate_id, assessment).result()

    def stop_generation(
        self, reason: str, next_action: Literal["provide_input", "try_another_scheme"], candidate_ids: list[str] | None = None
    ) -> str:
        """记录无法继续的原因并结束本轮, 不选择候选或派发新任务.

        Args:
            reason: 当前方案无法继续的具体原因.
            next_action: provide_input 表示需要补充输入, try_another_scheme 表示建议另建任务改试方案.
            candidate_ids: 与受阻判断有关的当前任务候选, 可以为空.

        Returns:
            受阻结束的确认, 或输入和候选引用无效的原因.
        """
        if self._worker is None:
            msg = "Render session is not open"
            raise RuntimeError(msg)
        return self._worker.submit(stop_generation, self, reason, next_action, candidate_ids).result()

    def outcome(self) -> GenerationOutcome:
        """提取运行结果.

        Returns:
            业务黑板、制品位置与完成状态.
        """
        return GenerationOutcome(
            state=self.state,
            run_dir=self.run_dir,
            selected_candidate=self.selected,
            stop_reason=self.stop_reason,
            attempts=self.attempts,
            model_calls=self.model_calls,
            baked_code_path=self.baked_code_path,
        )

    def _close_renderer(self) -> None:
        if self.renderer is not None:
            self.renderer.close()
            self.renderer = None

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        """在浏览器所属线程释放资源.

        Args:
            exc_type: with 内的异常类型.
            exc: with 内的异常.
            traceback: 异常堆栈.
        """
        worker, self._worker = self._worker, None
        # 先阻止新的工具调用, 再在原线程关闭浏览器, 最后等待线程池退出.
        # finally 确保浏览器关闭出错时仍会执行线程池清理.
        if worker is not None:
            try:
                worker.submit(self._close_renderer).result()
            finally:
                worker.shutdown(wait=True)
