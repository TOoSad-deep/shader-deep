"""工具绑定、运行快照和串行浏览器会话的生命周期."""

# RenderSession 是一次生成运行的工作台, 由 run_generation 的 with 语句管理.
# 生命周期: 创建会话 -> 启动单工作线程 -> 按需启动浏览器 -> 多次工具调用 -> 关闭资源.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal

from langchain.tools import BaseTool, tool

from shader_deep.agents.generation.contracts import GenerationOutcome
from shader_deep.agents.generation.tools.finish import select_candidate, stop_generation
from shader_deep.agents.generation.tools.render import render_candidate
from shader_deep.infrastructure.storage.artifacts import create_run_directory, save_run

if TYPE_CHECKING:
    # 这些类型仅供检查; 不在运行时互相导入, 避免 session 与执行模块形成循环导入.
    from pathlib import Path
    from types import TracebackType
    from typing import Self

    from shader_deep.agents.generation.options import GenerationOptions
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
        inputs: CapturedGenerationInputs | None = None,
    ) -> None:
        """绑定黑板和执行条件.

        Args:
            state: 本次任务的起始黑板.
            task_id: 生成任务标识.
            options: 固定渲染条件与预算.
            asset_root: 输入制品根目录, 写入运行记录供追溯.
            run_dir: 报告准备阶段已分配的运行目录; 未提供时创建新目录.
            inputs: 报告任务已固定的材料, 供逐轮请求复用.
        """
        self.state = state
        self.task_id = task_id
        self.options = options
        self.asset_root = asset_root
        self.run_dir = run_dir if run_dir is not None else create_run_directory(options.output_dir)
        self.inputs = inputs
        # 尝试次数限制渲染成本, 模型轮数限制持续对话; 两者独立累计.
        self.attempts = 0
        self.model_calls = 0
        self.stop_reason = "running"
        self.error: str | None = None
        self.selected: CandidateRecord | None = None
        # successful: 生成了 PNG; presented: 已将该 PNG 加入后续模型请求.
        # finish 要求同时满足两者, 防止模型在尚未收到预览时直接结束.
        self.presented: set[str] = set()
        self.successful: set[str] = set()
        self._worker: ThreadPoolExecutor | None = None
        self.renderer: WebGL2Renderer | None = None

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
        # Playwright 同步对象需要在所属线程使用和关闭.
        # max_workers=1 同时保证多次工具调用串行, 不并发修改本会话的尝试计数.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shader-render")
        return self

    def save(self) -> None:
        """保存本轮条件、预算、选择状态及黑板."""
        # 只把可序列化的运行信息交给 artifacts, 不保存浏览器、线程池和模型客户端.
        baseline = self.inputs.baseline if self.inputs is not None else None
        paths = (
            None
            if baseline is None
            else {"candidate_id": baseline.candidate_id, "code_path": baseline.code_path, "preview_path": baseline.preview_path}
        )
        save_run(
            self.run_dir,
            self.state,
            {
                "task_id": self.task_id,
                "asset_root": str(self.asset_root),
                "phase": "generation",
                "inputs": {"baseline": paths},
                "render": {"width": self.options.width, "height": self.options.height, "time": self.options.time},
                "max_attempts": self.options.max_attempts,
                "max_model_calls": self.options.max_attempts + 2,
                "attempts": self.attempts,
                "model_calls": self.model_calls,
                "stop_reason": self.stop_reason,
                "error": self.error,
                "selected_candidate_id": self.selected.id if self.selected is not None else None,
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
        return [tool("render_shader")(self.render_shader), tool("finish_shader")(self.finish_shader), tool("stop_generation")(self.stop_generation)]

    def render_shader(self, glsl_code: str) -> str:
        """编译并渲染完整 mainImage 代码, 下一轮将提供真实预览或编译错误.

        Args:
            glsl_code: 完整 ShaderToy 代码, 不声明版本、iResolution、iTime 或 main.

        Returns:
            候选 ID、执行状态和剩余尝试次数, 成功预览由 Context Builder 加载.
        """
        if self._worker is None:
            msg = "Render session is not open"
            raise RuntimeError(msg)
        # submit 在专用线程执行普通函数, result 等待结果并将异常传回调用方.
        # 模型传入的只有 glsl_code; 会话 self 由这一层补齐.
        return self._worker.submit(render_candidate, self, glsl_code).result()

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
