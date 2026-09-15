"""使用真实 Chromium WebGL2 渲染 ShaderToy 单 Pass 片段, 返回 PNG 字节."""

# Python 管理浏览器资源和参数传递; webgl2.js 在页面内负责实际编译、绘制与 PNG 导出.
# 渲染一帧的过程不请求模型, 也不评价结果与参考图的相似度.

from __future__ import annotations

import base64
import math
from contextlib import ExitStack
from importlib.resources import files
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from playwright.sync_api import Error as PlaywrightError, sync_playwright

if TYPE_CHECKING:
    from types import TracebackType
    from typing import Self

    from playwright.sync_api import Page


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


# 与 webgl2.js 返回的对象保持一致: 成功带 png, 失败带 stage/message.
# TypedDict/cast 只帮助静态检查理解返回结构, 不执行运行时数据校验.
class _Success(TypedDict):
    ok: Literal[True]
    png: str


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


class WebGL2Renderer:
    """可复用的同步 WebGL2 渲染器, 使用 with 管理浏览器生命周期.

    同一实例在创建它的线程内串行渲染多帧, 每次重新编译并释放 Shader 和 program.
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
                # 从 Python 包资源读取固定脚本, 安装 window.renderShader 入口.
                # 模型生成的 GLSL 稍后作为参数传入, 不作为 JavaScript 源码执行.
                page.evaluate(files("shader_deep.rendering").joinpath("webgl2.js").read_text(encoding="utf-8"))
                self._page = page
                # pop_all 将清理责任移交给实例, 防止离开局部 with 时立即关闭浏览器.
                self._resources = resources.pop_all()
        except (PlaywrightError, OSError) as exc:
            raise RenderError(str(exc), stage="init") from exc
        return self

    def render(self, glsl_code: str, width: int, height: int, time: float) -> bytes:
        """按指定像素尺寸和时间渲染一帧.

        Args:
            glsl_code: 包含 mainImage 的单 Pass ShaderToy 代码.
            width: 输出 PNG 的像素宽度.
            height: 输出 PNG 的像素高度.
            time: 直接传给 iTime 的秒数, 不使用墙上时钟或动画计时.

        Returns:
            PNG 编码字节, 保留 Shader 输出的透明度.

        Raises:
            ValueError: 代码为空、尺寸无效或时间不是有限数值.
            RenderError: 未启动渲染器, 或编译、链接、绘制、导出失败.
        """
        _validate(glsl_code, width, height, time)
        if self._page is None:
            msg = "Use WebGL2Renderer inside a with block"
            raise RenderError(msg, stage="init")
        try:
            # evaluate 跨 Python/浏览器边界发送结构化参数, 页面内部调用 WebGL2.
            # 时间使用传入的固定值, 不依赖程序执行耗时或系统当前时间.
            response = cast(
                "_Success | _Failure",
                self._page.evaluate(
                    "args => window.renderShader(args)",
                    {"glsl_code": glsl_code, "width": width, "height": height, "time": time},
                ),
            )
        except PlaywrightError as exc:
            raise RenderError(str(exc), stage="browser") from exc
        if not response["ok"]:
            # 保留编译、链接等阶段及原始日志, 上层 Tool 据此向模型反馈错误.
            raise RenderError(response["message"], stage=response["stage"])
        # 浏览器返回 data:image/png;base64,...; 去掉前缀并解码, 得到可写文件的 bytes.
        return base64.b64decode(response["png"].split(",", 1)[1], validate=True)

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
