# 渲染模块

本模块负责把单 Pass GLSL ES 3.00 Shader 绘制成 PNG, 并提供可持续调参的浏览器预览. 既可以由普通 Python 脚本调用, 也可以由 Agent 工具包装调用.

调用方统一从 `shader_deep.rendering` 导入. 安装与 Chromium 准备见[应用 README](../../../README.md#安装与配置); 本文的命令均在 `apps/shader_deep/` 目录执行. 渲染本身不需要模型配置或 API 密钥.

## 功能与选择

| 场景 | 使用入口 | 行为 |
| --- | --- | --- |
| 生成或验证不同 Shader 源码 | `WebGL2Renderer` | 一个浏览器可渲染多次, 每次重新编译并释放 program |
| 固定 Shader, 连续修改参数 | `ShaderPreview` | 常驻浏览器, 编译一次, 后续复用 program 更新 uniform |
| 手动体验滑块调参 | [preview.html](preview.html) | 直接打开默认演示, 提供光晕强度、线条宽度和重置按钮 |
| 读取当前渲染结果 | `ShaderPreview.capture()` | 同步绘制, 返回 PNG、完整参数、版本、实际时间和尺寸 |

PNG 直接从 WebGL 画布导出, 保留透明度及真实像素尺寸, 不包含网页控件或边距. 本模块不调用模型、不评价视觉相似度、不自动保存候选文件, 也不提供候选选择或业务预算管理.

## 快速开始: 常驻预览

以下示例可以保存为 `preview_example.py`, 用 `uv run --no-sync python preview_example.py` 运行:

```python
from pathlib import Path

from shader_deep.rendering import ParameterSpec, ShaderPreview

shader_code = """
uniform float RED;
uniform float GREEN;
void mainImage(out vec4 color, in vec2 pixel) {
    color = vec4(RED, GREEN, 0.0, 1.0);
}
"""
parameters: dict[str, ParameterSpec] = {
    "RED": {"min": 0.0, "max": 1.0, "initial": 0.2},
    "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.3},
}

with ShaderPreview(shader_code, parameters=parameters) as preview:
    current = preview.set_parameters({"RED": 0.8})
    # GREEN 保留当前值 0.3; current 是完整参数字典的副本.
    print(current)

    snapshot = preview.capture()
    Path("preview.png").write_bytes(snapshot.png)
    print(snapshot.revision, snapshot.time, dict(snapshot.parameters))

    # 保持 with 不退出, 可以在浏览器中继续手动拖动滑块.
    input("按回车关闭预览: ")
```

读取本地 Shader 时, 将 `shader_code` 换成 `Path("example.glsl").read_text(encoding="utf-8")`. 构造函数接收的是源码文本, 不是文件路径. 无界面运行时设置 `headless=True`, 并移除示例末尾的 `input()`.

## Shader 输入约定

提供如下入口及其辅助函数:

```glsl
uniform float GLOW_STRENGTH;

void mainImage(out vec4 color, in vec2 pixel) {
    vec2 uv = pixel / iResolution.xy;
    color = vec4(uv * GLOW_STRENGTH, 0.0, 1.0);
}
```

- 渲染器提供 `#version 300 es`、默认 precision、`main`、`iResolution` 和 `iTime`, 调用方不要重复声明.
- 可调参数使用独立的 `uniform float NAME;` 声明, 可包含 `lowp`、`mediump` 或 `highp` 精度修饰符.
- 当前自定义参数赋值支持标量 float; 不支持 int、向量、矩阵、数组、纹理通道或多 Pass 装配.
- 参数必须在链接后的程序中活跃. 未使用而被编译器优化掉的参数会返回 `uniform` 阶段错误.
- GLSL 语法、编译与链接由真实浏览器校验; Python 构造阶段的基本校验不代表 Shader 已可运行.

## 公开接口

```python
from shader_deep.rendering import (
    ParameterSpec,
    PreviewSnapshot,
    RenderError,
    ShaderPreview,
    WebGL2Renderer,
)
```

### ShaderPreview: 常驻会话

```text
ShaderPreview(
    glsl_code: str,
    *,
    parameters: Mapping[str, ParameterSpec],
    width: int = 512,
    height: int = 512,
    time: float | None = 0.0,
    headless: bool = False,
)
```

`Mapping` 来自 `collections.abc`. 除源码外, 构造参数均为关键字参数.

| 参数 | 含义与约束 |
| --- | --- |
| `glsl_code` | 非空源码文本, 会话内固定; 换源码需要新建会话 |
| `parameters` | 可调整参数的白名单、范围与初值; 允许空字典以预览固定 Shader |
| `width` / `height` | PNG 像素尺寸, 必须为正整数, 不接受布尔值; 超过 WebGL 限制时报错 |
| `time` | 有限数值表示固定 iTime; `None` 表示浏览器持续更新时间 |
| `headless` | 默认显示浏览器窗口; `True` 隐藏窗口, 仍执行真实 WebGL 绘制 |

构造只校验并复制配置. 进入 `with` 才启动独立 Chromium、加载页面、编译并检查初始绘制. 此接口不接管之前手动打开的浏览器标签页.

| 方法 | 返回值 | 行为 |
| --- | --- | --- |
| `set_parameters(parameters: Mapping[str, float])` | `dict[str, float]` | 校验并合并新值, 同步绘制及页面控件, 返回完整参数副本 |
| `capture()` | `PreviewSnapshot` | 重新绘制当前帧并直接导出 PNG |
| `close()` | `None` | 关闭浏览器及内部工作线程, 可重复调用 |

`parameter_specs` 属性返回允许参数的 `min/max/initial` 嵌套字典副本, 可用于工具说明或界面展示. 修改返回字典不会改变当前会话配置.

生命周期日志通过 `shader_deep.rendering._preview_browser` 输出, 含独立 session ID、Chromium 版本、页面关闭/崩溃和浏览器断开事件. `owner_closing=True` 表示本模块主动回收, 意外事件使用 WARNING 级别; 这些事件不能单独证明操作系统终止的具体原因. `with` 内已有异常时, 后续关闭异常以 exception note 附加, 保留主异常; 正常退出时关闭失败仍抛出. 本模块不自动重启会话.

初始化失败也保留最初的错误: 编译失败后即使浏览器和驱动清理再次失败, 调用方仍收到原 `RenderError` 的 stage/log, 各个清理错误写入 `__notes__`. 原始 Playwright/OS 初始化错误继续包装为 `stage="init"`, 清理说明同时保留在外层异常上. 初始化中断后同样尝试释放全部已获得资源并回收工作线程.

`set_parameters` 未提供的参数保留**当前值**, 不恢复 initial. 一组更新先通过名称/范围检查再应用; 有任一非法参数时整组拒绝. 空字典是允许的. 成功的更新调用使参数版本递增, 包括传入与当前值相同的数值; `capture()` 不递增版本.

### ParameterSpec: 参数配置

这是一个 TypedDict, 运行时使用普通字典, 三个字段均必填:

```python
spec: ParameterSpec = {"min": 0.0, "max": 3.0, "initial": 1.2}
```

| 字段 | 含义 |
| --- | --- |
| `min` | 最小值, 包含端点 |
| `max` | 最大值, 包含端点 |
| `initial` | 启动初值及页面重置值, 必须满足 `min <= initial <= max` |

名称须为合法标识符, 不能覆盖 `iTime` 或 `iResolution`. 数值不接受布尔值、字符串、NaN 或无穷大. 参数声明及链接后的实际类型还会在浏览器中校验. 配置会被复制, 外部随后修改原字典不改变当前会话的白名单与范围.

### PreviewSnapshot: 取图结果

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `png` | `bytes` | PNG 原始字节, 可用 `Path.write_bytes` 保存 |
| `parameters` | `Mapping[str, float]` | 该帧完整参数的独立只读副本; 使用 `dict(...)` 获取可修改/可 JSON 序列化的副本 |
| `revision` | `int` | 参数版本, 初始为 0; 是参数更新编号, 不是动画帧编号 |
| `time` | `float` | 该帧实际使用的 iTime 秒数 |
| `width` / `height` | `int` | PNG 像素宽度/高度 |

快照在后续调参或关闭浏览器后仍有效. `time=None` 时, 两次取图即使参数版本相同, 时间和图像也可能不同. 比较候选时建议固定时间; 复现需同时保留 Shader 源码、参数、尺寸和时间, PNG 本身不包含这些业务元数据.

### WebGL2Renderer: 单帧渲染

```text
WebGL2Renderer(*, channel: str = "chromium")
renderer.render(
    glsl_code: str,
    width: int,
    height: int,
    time: float,
    *,
    uniforms: dict[str, float] | None = None,
) -> bytes
renderer.close() -> None
```

此入口固定使用无头浏览器. 默认使用 Playwright 配套 Chromium; `channel="chrome"` 可选择本机 Chrome. `uniforms` 是本次绘制的数值, 不提供白名单/范围配置, 也不保留前次调用的值.

```python
from pathlib import Path

from shader_deep.rendering import WebGL2Renderer

shader_code = """
uniform float RED;
void mainImage(out vec4 color, in vec2 pixel) {
    color = vec4(RED, 0.0, 0.0, 1.0);
}
"""
with WebGL2Renderer() as renderer:
    png = renderer.render(shader_code, 512, 512, 0.0, uniforms={"RED": 0.8})
    Path("single-frame.png").write_bytes(png)
```

浏览器与画布在 `with` 内复用, 每次 `render` 都重新编译并释放 program. `close()` 幂等; 与 `ShaderPreview` 不同, 已关闭的 `WebGL2Renderer` 可以重新进入 `with` 启动.

## 生命周期与线程

- `ShaderPreview` 的创建、使用和关闭由内部单工作线程执行, 外部不同线程可以调用公开接口. 对同一会话的单次更新、取图和关闭操作串行执行.
- `set_parameters()` 加 `capture()` 是两个独立操作, 不保证这两次调用之间没有其他调用线程或手动滑块更新. 并行比较不同候选时, 应使用独立预览实例.
- `ShaderPreview` 退出 `with` 自动关闭, 启动失败也回收资源. 关闭后不能重新启动, 需要创建新实例. 调用前未进入 `with` 或关闭后再次调用会报错.
- `WebGL2Renderer` 不提供跨线程调度. 同一个实例应在同一线程创建、进入、绘制和关闭.
- 预览页面进入往返缓存时只暂停绘制, 恢复时继续使用原 program; 永久离开或显式关闭才释放资源. WebGL 上下文丢失时会报告错误并关闭当前预览, 暂不自动重建.

## 错误处理

| 异常 | 常见原因 |
| --- | --- |
| `ValueError` | 空源码、无效尺寸/时间、参数字段缺失或初值越界、非法名称/数值格式 |
| `RenderError` | Shader 编译/链接失败、未活跃或类型不符的 uniform、更新超范围、会话未启动/已关闭、浏览器错误 |

```python
from shader_deep.rendering import RenderError

try:
    preview.set_parameters({"RED": 2.0})
except ValueError as error:
    print("输入格式或配置错误:", error)
except RenderError as error:
    print("失败阶段:", error.stage)
    print("原始详情:", error.log)
```

示例中的 `preview` 应为当前仍打开的会话. 常见 `stage` 包括 `init`、`vertex_compile`、`fragment_compile`、`link`、`uniform`、`draw`、`export`、`preview`、`browser`、`close` 和 `protocol`; 其他底层阶段如 `context`、`size` 也会原样保留. 调用方应保留阶段及日志, 不仅判断报错文本.

## 与 Agent 工具的关系

渲染模块提供更新和取图能力. 工具层只需调用 `set_parameters` 或 `capture`, 再把返回的 PNG 包装成模型图片消息; 浏览器线程、uniform 赋值和 PNG 导出都由本模块负责.

参考图匹配任务由 `experiments/parameter_task.py` 的 `build_reference_task(reference_path)` 构建, 将只读目标 PNG 放入 HumanMessage. 本模块仍只负责当前预览, 不接收隐藏目标参数或执行匹配评估. 使用方式见[参考图匹配示例](../../../README.md#按参考图匹配).

`experiments/tools/` 的 `build_parameter_tools(preview)` 已包装两个轻量工具: `set_parameters` 更新参数并返回回执, `capture_preview` 返回该帧元数据及 PNG 图片消息. 每个工具独立一个文件, 由工厂装配; 旧 `parameter_tool.py` 只转发导入. 两者共享调用方管理的常驻会话, 不关闭预览、不写候选文件, 名称/范围从 `parameter_specs` 获取. Agent 必须在外层 with 结束前完成调用. 目录职责见[工具 README](../experiments/tools/README.md), 装配示例见[应用 README](../../../README.md#独立调参实验).

可选的 `build_parameter_tools(preview, reference_path=...)` 在实验工具层固定参考图, 另启用 `compare_preview` 和 `get_trial_history`. 并排图由 `imaging/comparison.py` 拼接, 历史由实验会话保存, 不给本渲染模块增加参考图或评分依赖. 两张图必须同尺寸, 不静默缩放; 工具只返回视觉材料与当前参数, 不计算/发送 MAE 或优劣结论. 详见[视觉反馈工具契约](../../../README.md#只读视觉反馈工具).

## 文件职责和依赖

| 文件 | 职责 |
| --- | --- |
| [__init__.py](__init__.py) | 统一公开导出 |
| [models.py](models.py) | 公共数据类型、快照不变性及渲染异常 |
| [_validation.py](_validation.py) | 单帧与常驻接口共享的输入校验 |
| [preview.py](preview.py) | Python 公开预览入口、调用顺序和线程生命周期 |
| [_preview_browser.py](_preview_browser.py) | 内部浏览器 Adapter、页面通信和 PNG 解码 |
| [renderer.py](renderer.py) | 单帧渲染及浏览器生命周期 |
| [webgl2.js](webgl2.js) | GPU 编译、绘制、uniform 更新及画布导出 |
| [preview.html](preview.html) | 页面结构、样式及默认演示 Shader |
| [preview.js](preview.js) | 页面配置装配、滑块及状态同步 |

`preview.py` 使用浏览器 Adapter 和数据契约; Adapter 不反向依赖公开会话. 两条 Python 渲染入口共用 models/validation, 不互相访问对方的私有函数. 下划线开头的文件是模块内部实现, 调用方不应直接导入.

## 验证与维护

在应用目录执行:

```sh
make check
make integration_test
```

前者覆盖离线测试、文档链接、导入约束、lint/格式和类型检查; 后者先验证浏览器可用, 再执行真实 WebGL 测试. [Python 预览测试](../../../tests/integration_tests/test_python_preview.py)通过公开接口验证参数、PNG 和生命周期; [页面测试](../../../tests/integration_tests/test_live_preview.py)覆盖滑块、状态同步和缓存生命周期; [单帧测试](../../../tests/integration_tests/test_renderer.py)覆盖原渲染接口. 缓存恢复测试使用模拟生命周期事件, 不代表真实导航已命中往返缓存.

修改公开接口、默认值、参数语义或生命周期时, 同步更新本文及行为测试. 修改静态资源时执行 `uv build`, 检查 wheel 包含 `preview.html`、`preview.js` 和 `webgl2.js`. 不把历史测试计数作为长期基线; 验证记录维护在[实验任务记录](../../../docs/work-items/parameter-agent-experiment.md).
