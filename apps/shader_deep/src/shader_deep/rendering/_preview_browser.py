"""常驻预览的浏览器 Adapter, 仅在会话所属线程使用.

负责启动页面、传递已编码配置、转换页面结果和关闭资源; 不管理调用线程或模型工具.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from shader_deep.rendering.models import PreviewSnapshot, RenderError

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from shader_deep.rendering.models import _PreviewConfig


class _State(TypedDict):
    revision: int
    parameters: dict[str, float]
    png: NotRequired[str]
    time: NotRequired[float]


class _Success(TypedDict):
    ok: Literal[True]
    data: _State


class _Failure(TypedDict):
    ok: Literal[False]
    stage: str
    log: str


# 这是与同包 preview.js/webgl2.js 的内部通信协议, 方法名不接受调用方输入.
_REQUEST = """({method, parameters}) => {
    if (window.shaderPreviewError) return {ok: false, ...window.shaderPreviewError};
    try { return {ok: true, data: window.shaderPreview[method](parameters)}; }
    catch (error) { return {ok: false, stage: error.stage || 'preview', log: error.message}; }
}"""

logger = logging.getLogger(__name__)


def _close_after_error(resources: ExitStack, primary: BaseException) -> None:
    """继续释放所有资源, 将清理错误保留为主异常的附加说明."""
    try:
        resources.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # 保留已有初始化/中断异常, 调用方随后重新抛出主异常.
        # ExitStack 会继续清理后续资源, 多个失败通过 __context__ 相连.
        # 原异常也可能在链尾, 不能把它再次记作清理错误.
        current: BaseException | None = cleanup_error
        seen = {id(primary)}
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            primary.add_note(f"Preview initialization cleanup also failed: {type(current).__name__}: {current}")
            current = current.__context__


class BrowserPreview:
    """一个浏览器与页面的所有者, 不向外部调用方暴露 Page 对象."""

    def __init__(self, config: _PreviewConfig) -> None:
        """保存已经校验的配置, 不启动资源.

        Args:
            config: 会话私有的配置副本.
        """
        self._config = config
        self._resources: ExitStack | None = None
        self._page: Page | None = None
        self._session_id = uuid4().hex
        self._closing = False

    def start(self) -> None:
        """启动页面并验证初始绘制, 失败时在当前线程释放已获得的资源."""
        resources = ExitStack()
        try:
            self._open(resources)
            # 成功后直接持有同一资源栈, 不创建一次可能失败的资源交接.
            self._resources = resources
        except BaseException as exc:
            self._closing = True
            _close_after_error(resources, exc)
            self._resources = None
            self._page = None
            if isinstance(exc, (PlaywrightError, OSError)):
                error = RenderError(str(exc), stage="init")
                for note in getattr(exc, "__notes__", ()):
                    error.add_note(note)
                raise error from exc
            raise

    def _open(self, resources: ExitStack) -> None:
        playwright = resources.enter_context(sync_playwright())
        browser = playwright.chromium.launch(channel="chromium", headless=self._config.headless, chromium_sandbox=True)
        resources.callback(browser.close)
        browser.on("disconnected", lambda _: self._lifecycle("browser_disconnected"))
        self._page = browser.new_page(viewport={"width": 1100, "height": 800})
        self._page.on("close", lambda _: self._lifecycle("page_closed"))
        self._page.on("crash", lambda _: self._lifecycle("page_crashed"))
        logger.info("Preview session=%s started chromium=%s", self._session_id, browser.version)
        self._configure_page(self._page)
        self._request("capture")

    def _configure_page(self, page: Page) -> None:
        config = self._config
        payload = {
            "glsl_code": config.glsl_code,
            "parameters": config.parameters,
            "width": config.width,
            "height": config.height,
            "time": config.time,
        }
        # 数据作为 JSON 字面量传递, 不将 Shader 插入 HTML 或作为 JS 执行.
        page.add_init_script("window.shaderPreviewConfig = " + json.dumps(payload) + ";")
        page.goto(Path(__file__).with_name("preview.html").resolve().as_uri())

    def _lifecycle(self, event: str) -> None:
        # 区分 owner 正常释放与意外关闭; 日志不包含源码、图像或密钥.
        level = logging.INFO if self._closing else logging.WARNING
        logger.log(level, "Preview session=%s event=%s owner_closing=%s", self._session_id, event, self._closing)

    def _request(self, method: Literal["capture", "setParameters"], parameters: dict[str, float] | None = None) -> _State:
        if self._page is None:
            msg = "Preview is not open"
            raise RenderError(msg, stage="preview")
        try:
            response = cast("_Success | _Failure", self._page.evaluate(_REQUEST, {"method": method, "parameters": parameters}))
        except PlaywrightError as exc:
            raise RenderError(str(exc), stage="browser") from exc
        if not response["ok"]:
            raise RenderError(response["log"], stage=response["stage"])
        return response["data"]

    def set_parameters(self, parameters: dict[str, float]) -> dict[str, float]:
        """请求页面原子更新参数, 返回完整参数副本.

        Args:
            parameters: 已通过基础格式校验的新值.

        Returns:
            页面接受更新后的完整参数.
        """
        return self._request("setParameters", parameters)["parameters"]

    def capture(self) -> PreviewSnapshot:
        """取回同一次绘制的图像、参数及时间, 并转换为 Python 快照.

        Returns:
            包含独立只读参数副本的 PNG 快照.
        """
        state = self._request("capture")
        try:
            png = base64.b64decode(state["png"].split(",", 1)[1], validate=True)
            return PreviewSnapshot(
                png=png,
                parameters=state["parameters"],
                revision=state["revision"],
                time=state["time"],
                width=self._config.width,
                height=self._config.height,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            msg = f"Invalid preview snapshot response: {exc}"
            raise RenderError(msg, stage="protocol") from exc

    def close(self) -> None:
        """在浏览器所属线程关闭页面、GPU 资源和驱动, 可重复调用."""
        self._closing = True
        try:
            if self._resources is not None:
                self._resources.close()
        except PlaywrightError as exc:
            raise RenderError(str(exc), stage="close") from exc
        finally:
            self._resources = None
            self._page = None
