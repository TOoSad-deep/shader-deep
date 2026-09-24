"""运行单 Agent 的生成、渲染、查看预览与修正闭环."""

# 对外入口分两组:
# run_shader / run_generation 返回完整 GenerationOutcome, 可检查未完成的制品;
# generate_shader / generate_task 只返回选定源码, 未完成时抛出携带 outcome 的异常.
# shader 入口从 PNG 建立初始任务, task/generation 入口处理调用方已有的黑板任务.

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from shader_deep.agents.generation.agent import _execute
from shader_deep.agents.generation.context import build_generation_context, png_data_url as png_data_url
from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.agents.generation.tools import GenerationLimitError, GenerationOutcome, RenderSession
from shader_deep.domain.blackboard import add_target, add_task, new_blackboard
from shader_deep.domain.tasks import TargetRecord, TaskRecord

if TYPE_CHECKING:
    from shader_deep.domain.tasks import BlackboardState

# 角色提示说明工作方式; 次数、工具白名单和选择资格另由运行时代码检查.
# 保护特征与视觉相似度仍依靠模型自检和用户验收, 当前没有程序化视觉验收器.


class GenerationIncompleteError(RuntimeError):
    """本轮没有选定可交付的已渲染候选."""

    def __init__(self, outcome: GenerationOutcome) -> None:
        """保存未完成运行的位置.

        Args:
            outcome: 含停止原因和已有制品的运行结果.
        """
        self.outcome = outcome
        super().__init__(f"Generation incomplete ({outcome.stop_reason}); artifacts: {outcome.run_dir}")


def normalize_glsl(response: str) -> str:
    """移除包裹完整回复的 GLSL Markdown 代码围栏.

    Args:
        response: 模型返回的原始文本.

    Returns:
        去掉完整 GLSL 外层围栏的内容, 或未经修改的原始文本.
    """
    stripped = response.strip()
    if stripped.startswith("```glsl") and stripped.endswith("```"):
        return stripped.removeprefix("```glsl").removesuffix("```").strip()
    return response


def run_generation(
    state: BlackboardState, task_id: str, *, asset_root: Path | None = None, options: GenerationOptions | None = None
) -> GenerationOutcome:
    """执行有预算的生成闭环并保存每次尝试.

    Args:
        state: 起始黑板.
        task_id: 已登记的生成任务.
        asset_root: 输入制品根目录, 未提供时使用当前目录.
        options: 渲染条件、尝试上限和输出目录, 未提供时使用默认配置.

    Returns:
        最新黑板、候选选择和运行目录; 达到预算时返回未选定候选的结果.

    Raises:
        KeyError: 任务引用不存在.
        ValueError: 任务、图片或模型配置无效.
        OSError: 输入或输出文件不可访问.
    """
    root = (asset_root if asset_root is not None else Path.cwd()).resolve()
    # 预先读取一次材料, 在创建会话和请求模型之前暴露缺失文件、错误角色等问题.
    # 这里的返回值不复用; 真正请求前, Context middleware 会重新读取最新状态.
    build_generation_context(state, task_id, asset_root=root)
    with RenderSession(state, task_id, options or GenerationOptions(), root) as session:
        try:
            _execute(session)
        except GenerationLimitError:
            # 达到次数预算是可预期的停止方式, 仍返回已有尝试供调用方检查.
            pass
        finally:
            # 选择成功或预算耗尽会提前设置停止原因; 其他离开路径登记为 error.
            # 未捕获的异常在保存之后继续抛出, with 负责关闭浏览器和执行线程.
            if session.stop_reason == "running":
                session.stop_reason = "error"
            session.save()
        return session.outcome()


def _completed_code(outcome: GenerationOutcome) -> str:
    if outcome.selected_candidate is None:
        raise GenerationIncompleteError(outcome)
    # 读取已选版本的制品, 不采用模型另外回复的一段未经渲染的代码.
    return Path(outcome.selected_candidate.code_path).read_text(encoding="utf-8")


def generate_task(state: BlackboardState, task_id: str, *, asset_root: Path | None = None, options: GenerationOptions | None = None) -> str:
    """执行生成任务并返回选定候选的实际代码.

    Args:
        state: 起始黑板.
        task_id: 生成任务标识.
        asset_root: 输入制品根目录.
        options: 可选的渲染和预算配置.

    Returns:
        已成功渲染并选定的候选代码.

    Raises:
        GenerationIncompleteError: 达到预算仍未选定已渲染候选, 已有尝试保存在运行目录.
    """
    return _completed_code(run_generation(state, task_id, asset_root=asset_root, options=options))


def run_shader(path: Path, prompt: str, *, options: GenerationOptions | None = None) -> GenerationOutcome:
    """从 PNG 和用户要求创建初始任务, 返回完整运行结果.

    Args:
        path: 本地 PNG 路径.
        prompt: 用户的生成要求.
        options: 可选的渲染和预算配置.

    Returns:
        含候选、预览位置和完成状态的运行结果.

    Raises:
        ValueError: 提示词为空或输入无效.
    """
    if not prompt.strip():
        msg = "Prompt must not be empty"
        raise ValueError(msg)
    # 便捷入口每次从空黑板开始, 因而 T1/G1 在本次状态中唯一.
    # 使用已有项目状态继续工作时, 应调用 run_generation 并传入明确的 task_id.
    state = add_target(new_blackboard(), TargetRecord(version="T1", request=prompt, reference_path=str(path.resolve())))
    state = add_task(state, TaskRecord(id="G1", role="generation", target_version="T1", objective="根据用户要求生成整图并渲染自检"))
    return run_generation(state, "G1", options=options)


def generate_shader(path: Path, prompt: str, *, options: GenerationOptions | None = None) -> str:
    """兼容原 PNG 加提示词接口, 返回选定的已渲染 GLSL.

    Args:
        path: 本地 PNG 路径.
        prompt: 用户的生成要求.
        options: 可选配置; 原来的两个位置参数仍然有效.

    Returns:
        已成功渲染并经过本轮自检选择的代码.

    Raises:
        GenerationIncompleteError: 运行未完成, 可通过异常的 outcome 查询已有制品.
        ValueError: 提示词或输入无效.
    """
    return _completed_code(run_shader(path, prompt, options=options))
