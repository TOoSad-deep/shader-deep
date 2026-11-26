"""使用真实 Chromium WebGL2 渲染 ShaderToy 单 Pass 片段."""

# Python 管理浏览器资源和参数传递; webgl2.js 在页面内负责实际编译、绘制与 PNG 导出.
# 渲染一帧的过程不请求模型, 也不评价结果与参考图的相似度.

from __future__ import annotations

import base64
import math
import re
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from importlib.resources import files
from typing import TYPE_CHECKING, Generic, Literal, TypedDict, TypeVar, cast

from playwright.sync_api import Error as PlaywrightError, sync_playwright

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from types import TracebackType
    from typing import Self

    from playwright.sync_api import Page

UniformValue = float | list[float] | tuple[float, ...]
_Region = tuple[int, int, int, int]
_UNIFORM_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_FLOAT32_MAX = 3.4028234663852886e38
_RECTANGLE_COORDINATES = 4
_Result = TypeVar("_Result")


class RenderError(RuntimeError):
    """渲染失败, 保留失败阶段与浏览器原始日志.

    Attributes:
        stage: 初始化、编译、链接、绘制或导出等失败阶段.
        log: 底层错误详情, Shader 编译错误保留驱动返回的日志.
    """

    def __init__(self, log: str, *, stage: str) -> None:
        """创建可供调用方诊断的渲染错误.

        Args:
            log: 该阶段的错误详情.
            stage: 失败阶段.
        """
        self.stage = stage
        self.log = log
        super().__init__(f"{stage}: {log}")


# 与 webgl2.js 返回的对象保持一致. 成功值随浏览器操作而异.
class _Success(TypedDict, Generic[_Result]):
    ok: Literal[True]
    value: _Result


class _Failure(TypedDict):
    ok: Literal[False]
    stage: str
    message: str


def _validate(glsl_code: str, width: int, height: int, time: float) -> None:
    # 渲染器可以脱离 GenerationOptions 单独使用, 所以保留自身入口校验.
    # 这里只检查通用参数, 具体 GLSL 语法和显卡尺寸上限交给浏览器检查.
    if not isinstance(glsl_code, str) or not glsl_code.strip():
        msg = "glsl_code must be a non-empty string"
        raise ValueError(msg)
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"{name} must be a positive integer"
            raise ValueError(msg)
    if isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time):
        msg = "time must be a finite number of seconds"
        raise ValueError(msg)


def _uniform_components(name: str, value: UniformValue) -> tuple[float, ...]:
    if isinstance(value, (list, tuple)):
        if len(value) not in (2, 3, 4):
            msg = f"Uniform {name!r} must have 2, 3, or 4 vector components"
            raise ValueError(msg)
        components = value
    else:
        components = (value,)
    if any(isinstance(part, bool) or not isinstance(part, (int, float)) or not -_FLOAT32_MAX <= part <= _FLOAT32_MAX for part in components):
        msg = f"Uniform {name!r} requires finite float32 components"
        raise ValueError(msg)
    return tuple(float(part) for part in components)


def _uniform_entries(uniforms: Mapping[str, UniformValue] | None) -> dict[str, tuple[float, ...]]:
    if uniforms is None:
        return {}
    if not isinstance(uniforms, Mapping):
        msg = "uniforms must be a mapping of GLSL names to scalar or vector values"
        raise TypeError(msg)
    entries = {}
    for name, value in uniforms.items():
        if not isinstance(name, str) or not _UNIFORM_NAME.fullmatch(name) or name.startswith("gl_") or "__" in name:
            msg = f"Invalid GLSL uniform name: {name!r}"
            raise ValueError(msg)
        if name in {"iResolution", "iTime"}:
            msg = f"Uniform {name!r} is reserved by the renderer"
            raise ValueError(msg)
        entries[name] = _uniform_components(name, value)
    return entries


def _evaluate(page: Page, operation: str, args: object, result_type: type[_Result]) -> _Result:
    try:
        # GLSL 和 uniform 值作为结构化数据跨越浏览器边界, JS 始终来自包内固定脚本.
        response = cast(
            "_Success[_Result] | _Failure",
            page.evaluate("([op, args]) => window.shaderRenderer(op, args)", [operation, args]),
        )
    except PlaywrightError as exc:
        raise RenderError(str(exc), stage="browser") from exc
    if not response["ok"]:
        raise RenderError(response["message"], stage=response["stage"])
    if not isinstance(response["value"], result_type):
        msg = f"Invalid browser response for {operation}"
        raise RenderError(msg, stage="browser")
    return response["value"]


class OptimizationSession:
    """在固定画布和时间下复用同一个已链接程序, 在其 with 块内使用."""

    def __init__(self, page: Page, width: int, height: int, uniform_sizes: dict[str, int]) -> None:
        """由 WebGL2Renderer 创建, 持有会话的固定渲染条件.

        Args:
            page: 已启动优化会话的浏览器页面.
            width: 画布宽度.
            height: 画布高度.
            uniform_sizes: 各 uniform 的固定分量数.
        """
        self._page: Page | None = page
        self._width = width
        self._height = height
        self._uniform_sizes = uniform_sizes

    def _trial(self, uniforms: Mapping[str, UniformValue]) -> tuple[Page, dict[str, tuple[float, ...]]]:
        if self._page is None:
            msg = "Optimization session is closed"
            raise RenderError(msg, stage="session")
        entries = _uniform_entries(uniforms)
        if {name: len(components) for name, components in entries.items()} != self._uniform_sizes:
            msg = "Trial uniforms must keep the session's names and component counts"
            raise ValueError(msg)
        return self._page, entries

    def render(self, uniforms: Mapping[str, UniformValue]) -> bytes:
        """导出少数正式候选的完整 PNG.

        Args:
            uniforms: 本次绘制的全部参数值, 名称与类型沿用会话初值.

        Returns:
            带透明度的 PNG 字节.

        Raises:
            ValueError: 参数无效或与初值的名称、类型不同.
            RenderError: 会话已关闭或绘制、导出失败.
        """
        page, entries = self._trial(uniforms)
        png = _evaluate(page, "png", list(entries.items()), str)
        return base64.b64decode(png, validate=True)

    def read_pixels(self, uniforms: Mapping[str, UniformValue]) -> bytes:
        """读取本次绘制的整幅画布, 按 PNG 的方向返回原始 RGBA 字节.

        Args:
            uniforms: 本次绘制的全部参数值, 名称与类型沿用会话初值.

        Returns:
            从左上角起按行排列的 RGBA 字节, 未执行 PNG 编码.
        """
        (pixels,) = self.read_regions(uniforms, [(0, 0, self._width, self._height)])
        return pixels

    def read_regions(self, uniforms: Mapping[str, UniformValue], regions: Sequence[_Region]) -> tuple[bytes, ...]:
        """只绘制一帧, 读取多个目标或保护区的原始 RGBA 字节.

        Args:
            uniforms: 本次绘制的全部参数值, 名称与类型沿用会话初值.
            regions: 左、上、右、下像素边界, 右下边界排除在外, 与 Pillow 一致.

        Returns:
            与输入区域同序的 RGBA 字节, 每区从左上角起按行排列.

        Raises:
            ValueError: 参数或区域无效.
            RenderError: 会话已关闭或绘制、读回失败.
        """
        page, entries = self._trial(uniforms)
        if not regions or any(not self._contains(region) for region in regions):
            msg = "Each read region must be a non-empty integer rectangle inside the canvas"
            raise ValueError(msg)
        samples = _evaluate(page, "read", {"uniforms": list(entries.items()), "regions": list(regions)}, list)
        return tuple(base64.b64decode(sample, validate=True) for sample in samples)

    def _contains(self, region: _Region) -> bool:
        if len(region) != _RECTANGLE_COORDINATES or any(isinstance(coord, bool) or not isinstance(coord, int) for coord in region):
            return False
        left, top, right, bottom = region
        return 0 <= left < right <= self._width and 0 <= top < bottom <= self._height

    def _close(self, renderer_page: Page | None) -> None:
        page, self._page = self._page, None
        if page is not None and page is renderer_page:
            _evaluate(page, "end", None, type(None))


class WebGL2Renderer:
    """可复用的同步 WebGL2 渲染器, 使用 with 管理浏览器生命周期.

    同一实例在创建它的线程内串行渲染多帧. 普通渲染逐帧编译,
    优化会话在固定尺寸与时间下复用同一个已链接程序.
    glsl_code 提供 GLSL ES 3.00 的 mainImage 函数与辅助代码;
    版本、precision、iResolution、iTime 和 main 入口由模块提供.
    """

    def __init__(self, *, channel: str = "chromium") -> None:
        """配置渲染器, 在进入 with 时启动浏览器.

        Args:
            channel: 默认使用 Playwright 配套 Chromium 的新无头模式;
                可显式指定 chrome 使用本机 Chrome.
        """
        self._channel = channel
        # 构造对象不启动浏览器; _resources / _page 为 None 表示尚未进入渲染会话.
        self._resources: ExitStack | None = None
        self._page: Page | None = None

    def __enter__(self) -> Self:
        """启动独立浏览器与 WebGL2 画布.

        Returns:
            已就绪的渲染器.

        Raises:
            RenderError: 实例已启动、浏览器不可用或无法创建 WebGL2 上下文.
        """
        if self._resources is not None:
            msg = "Renderer is already open"
            raise RenderError(msg, stage="init")
        try:
            with ExitStack() as resources:
                # 每获得一个资源就登记清理动作; 初始化中途失败也能按逆序释放.
                # browser.close 后退出 sync_playwright, 避免残留驱动与浏览器进程.
                playwright = resources.enter_context(sync_playwright())
                browser = playwright.chromium.launch(channel=self._channel, headless=True, chromium_sandbox=True)
                resources.callback(browser.close)
                page = browser.new_page()
                page.set_content("<!doctype html><html><body></body></html>")
                # 从 Python 包资源读取固定脚本, 安装 window.shaderRenderer 入口.
                # 模型生成的 GLSL 稍后作为参数传入, 不作为 JavaScript 源码执行.
                page.evaluate(files("shader_deep.rendering").joinpath("webgl2.js").read_text(encoding="utf-8"))
                self._page = page
                # pop_all 将清理责任移交给实例, 防止离开局部 with 时立即关闭浏览器.
                self._resources = resources.pop_all()
        except (PlaywrightError, OSError) as exc:
            raise RenderError(str(exc), stage="init") from exc
        return self

    def render(self, glsl_code: str, width: int, height: int, time: float, *, uniforms: Mapping[str, UniformValue] | None = None) -> bytes:
        """按指定像素尺寸和时间渲染一帧.

        Args:
            glsl_code: 包含 mainImage 的单 Pass ShaderToy 代码.
            width: 输出 PNG 的像素宽度.
            height: 输出 PNG 的像素高度.
            time: 直接传给 iTime 的秒数, 不使用墙上时钟或动画计时.
            uniforms: 可选的 GLSL float、vec2、vec3、vec4 参数及实际值.

        Returns:
            PNG 编码字节, 保留 Shader 输出的透明度.

        Raises:
            ValueError: 代码、尺寸、时间或参数值无效.
            TypeError: uniforms 不是键值映射.
            RenderError: 未启动渲染器, 或编译、链接、参数绑定、绘制、导出失败.
        """
        _validate(glsl_code, width, height, time)
        entries = _uniform_entries(uniforms)
        png = _evaluate(
            self._open_page(),
            "render",
            {"code": glsl_code, "width": width, "height": height, "time": time, "uniforms": list(entries.items())},
            str,
        )
        return base64.b64decode(png, validate=True)

    @contextmanager
    def optimization_session(
        self, glsl_code: str, width: int, height: int, time: float, *, uniforms: Mapping[str, UniformValue]
    ) -> Iterator[OptimizationSession]:
        """链接一次 GLSL, 让同一着色程序复用到多次试验和正式导出.

        Args:
            glsl_code: 包含 mainImage 的单 Pass ShaderToy 代码.
            width: 固定画布宽度.
            height: 固定画布高度.
            time: 固定渲染时间, 单位为秒.
            uniforms: 用来验证初始名称和类型的参数值.

        Yields:
            独占当前渲染器, 且仅在本 with 块内有效的优化会话.

        Raises:
            ValueError: 输入无效.
            TypeError: uniforms 不是键值映射.
            RenderError: 渲染器未开启、被另一会话占用或程序无法编译和绑定.
        """
        _validate(glsl_code, width, height, time)
        entries = _uniform_entries(uniforms)
        page = self._open_page()
        args = {"code": glsl_code, "width": width, "height": height, "time": time, "uniforms": list(entries.items())}
        _evaluate(page, "start", args, type(None))
        session = OptimizationSession(page, width, height, {name: len(components) for name, components in entries.items()})
        try:
            yield session
        except BaseException as exc:
            try:
                session._close(self._page)
            except RenderError as cleanup_error:
                exc.add_note(f"Optimization session cleanup failed: {cleanup_error}")
            raise
        else:
            session._close(self._page)

    def _open_page(self) -> Page:
        if self._page is None:
            msg = "Use WebGL2Renderer inside a with block"
            raise RenderError(msg, stage="init")
        return self._page

    def close(self) -> None:
        """释放浏览器和驱动, 已关闭时可以重复调用."""
        resources, self._resources = self._resources, None
        # 先清除实例引用, 让重复 close 成为无操作; 下次 __enter__ 可重新建立会话.
        self._page = None
        if resources is not None:
            resources.close()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        """退出 with 时释放资源.

        Args:
            exc_type: with 内发生的异常类型.
            exc: with 内发生的异常.
            traceback: 异常堆栈.
        """
        self.close()
