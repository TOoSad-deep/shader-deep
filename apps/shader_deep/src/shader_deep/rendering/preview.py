"""常驻预览的公共入口, 只负责调用顺序、串行执行和线程生命周期.

浏览器资源与页面通信在 _preview_browser 中实现; 调用方无需操作 Playwright.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Lock
from typing import TYPE_CHECKING, TypeVar

from shader_deep.rendering._preview_browser import BrowserPreview
from shader_deep.rendering._validation import copy_parameter_specs, validate_render_input, validate_uniform_values
from shader_deep.rendering.models import ParameterSpec, PreviewSnapshot, RenderError, _PreviewConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import TracebackType
    from typing import Self

_Result = TypeVar("_Result")


class ShaderPreview:
    """持有一个浏览器和已编译 Shader 的预览会话.

    使用 with 启动, 可由不同调用线程更新/取图, 内部串行访问同一页面.
    close 幂等; 已关闭的实例不能重新启动. 每个实例拥有独立浏览器.
    """

    def __init__(
        self,
        glsl_code: str,
        *,
        parameters: Mapping[str, ParameterSpec],
        width: int = 512,
        height: int = 512,
        time: float | None = 0.0,
        headless: bool = False,
    ) -> None:
        """固定源码和参数配置, 暂不启动浏览器.

        Args:
            glsl_code: 含 mainImage 和自定义 uniform float 声明的源码.
            parameters: 可修改的参数名称、范围和初值; 可为空以预览固定 Shader.
            width: 输出像素宽度.
            height: 输出像素高度.
            time: 固定 iTime 秒数; None 时随浏览器时间连续变化.
            headless: 是否隐藏浏览器窗口, 自动化测试可设置为 True.

        Raises:
            ValueError: 代码、尺寸、时间或参数配置无效.
        """
        validate_render_input(glsl_code, width, height, 0.0 if time is None else time)
        config = _PreviewConfig(glsl_code, copy_parameter_specs(parameters), width, height, time, headless)
        self._parameter_specs = config.parameters
        self._browser = BrowserPreview(config)
        self._lock = Lock()
        self._worker: ThreadPoolExecutor | None = None
        self._closed = False

    @property
    def parameter_specs(self) -> dict[str, ParameterSpec]:
        """返回允许参数的配置副本, 供调用方展示名称、范围和初值.

        Returns:
            独立的嵌套字典, 修改它不会改变会话配置或权限.
        """
        return {name: ParameterSpec(**spec) for name, spec in self._parameter_specs.items()}

    def __enter__(self) -> Self:
        """启动浏览器并编译 Shader, 失败时回收线程和浏览器.

        Returns:
            可调用的当前会话.

        Raises:
            RenderError: 已启动、已关闭或浏览器/Shader 初始化失败.
        """
        with self._lock:
            if self._closed or self._worker is not None:
                msg = "Preview is already started or closed"
                raise RenderError(msg, stage="init")
            self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shader-preview")
            try:
                self._worker.submit(self._browser.start).result()
            except BaseException as primary:
                # 即使清理浏览器时再次失败, finally 也必须回收工作线程.
                worker, self._worker = self._worker, None
                self._closed = True
                try:
                    worker.submit(self._browser.close).result()
                except BaseException as cleanup_error:  # noqa: BLE001  # 主异常会在收回线程后重新抛出, 清理失败不能覆盖它.
                    primary.add_note(f"Preview initialization cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}")
                finally:
                    worker.shutdown(wait=True)
                raise
        return self

    def _run(self, operation: Callable[[], _Result]) -> _Result:
        # 锁覆盖提交和等待, close 不会在更新或取图的中途关闭浏览器.
        with self._lock:
            if self._worker is None or self._closed:
                msg = "Preview is not open; use with ShaderPreview(...)"
                raise RenderError(msg, stage="preview")
            return self._worker.submit(operation).result()

    def set_parameters(self, parameters: Mapping[str, float]) -> dict[str, float]:
        """局部更新允许的参数, 同步画布和控件.

        Args:
            parameters: 本次参数新值, 未提供的参数保留当前值.

        Returns:
            更新后的完整参数副本.

        Raises:
            ValueError: 参数名称或数值格式无效.
            RenderError: 越界、名称不在白名单、会话未启动/已关闭或浏览器失败.
        """
        values = dict(parameters)
        validate_uniform_values(values)
        return self._run(partial(self._browser.set_parameters, values))

    def capture(self) -> PreviewSnapshot:
        """同步绘制并取回当前 PNG 及该帧对应的参数和时间.

        Returns:
            不受后续参数修改影响的只读快照.

        Raises:
            RenderError: 会话未启动/已关闭, 或绘制、取图失败.
        """
        return self._run(self._browser.capture)

    def close(self) -> None:
        """关闭浏览器并等待内部线程退出, 可重复调用.

        Raises:
            RenderError: 关闭浏览器时发生底层错误.
        """
        with self._lock:
            self._closed = True
            if self._worker is None:
                return
            worker, self._worker = self._worker, None
            try:
                worker.submit(self._browser.close).result()
            finally:
                worker.shutdown(wait=True)

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        """退出 with 时关闭预览并释放线程.

        Args:
            exc_type: 异常类型.
            exc: 异常实例.
            traceback: 异常堆栈.
        """
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is None:
                raise
            # with 内已有主异常时, 清理故障只附加说明, 不替换根因.
            exc.add_note(f"Preview cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}")
