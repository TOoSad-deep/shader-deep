# Shader Deep

输入本地 PNG 和文字要求, 通过单个 Deep Agent 生成、渲染、查看预览并修正 Shader, 将选定候选的实际 GLSL 写入标准输出。

当前已连接内存黑板、Context Builder、真实 WebGL2 渲染工具和有预算的生成循环。每次尝试保存代码、预览或错误, 完成时明确选择已渲染且已向模型展示预览的候选。

独立多视角分析已提供 `shader-deep-analyze` 命令: 主 Agent 选择预置视角或创建新视角配置, 执行器并行运行独立分析实例, 保存原始报告与带证据来源的综合结果。分析到交付报告为止; 持续决策、独立视觉评审和分析到生成的自动交接尚未接入。最终视觉效果仍由用户验收。

## 安装与配置

在本目录操作, 使用仓库约定的 `uv` 管理独立环境:

```sh
cd /Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep
make sync
```

应用以可编辑包安装到本目录的 `.venv`。`deepagents` 继续使用 `../../libs/deepagents` 的本地可编辑依赖。应用直接声明 LangChain 依赖以使用其消息和 middleware 接口, 版本范围与当前 SDK 一致。渲染模块使用 Microsoft 维护的 Playwright 及其配套 Chromium; Pillow 仅用于测试中解码 PNG、核对像素。构建采用 setuptools, Ruff 和 ty 仅作为开发检查工具。

使用渲染模块前, 安装与锁定 Playwright 版本匹配的浏览器:

```sh
make browsers
```

安装命令保留其他项目的浏览器缓存。浏览器准备好后, 渲染本身不需要网络或模型配置。

已有 `.env` 时继续使用。首次配置可根据 `.env.example` 新建 `.env`, 填入以下三项:

| 变量 | 内容 |
| --- | --- |
| `DS_MICU_MODEL` | DeepSeek 专用配置的模型 ID |
| `DS_MICU_BASE_URL` | 该配置组的 API 地址 |
| `DS_MICU_API_KEY` | 该配置组的 API 密钥 |

默认优先使用 `DS_MICU_*` 整组配置。只有这三个变量全部未配置或为空白时, 才使用既有的 `MICU_MODEL`、`MICU_BASE_URL`、`MICU_API_KEY`。选中 `DS_MICU_*` 后缺少任一项会直接报错, 不会从 `MICU_*` 借用密钥或地址。这样可以在同一 `.env` 中保留不同模型通道的两套凭证。

程序读取进程环境变量, `.env` 由运行命令中的 `--env-file` 加载。实际 `.env` 文件继续由 Git 忽略。

## LangSmith 追踪

使用 LangChain 原生追踪, 无需额外安装依赖。在本目录的 `.env` 中添加:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=你的_LangSmith_密钥
LANGSMITH_PROJECT=shader-deep
```

然后继续使用下面带 `--env-file .env` 的运行命令。在 LangSmith 的 `shader-deep` 项目中查找 `shader-deep.generation`, 可以查看模型输入输出、工具调用、耗时和模型返回的 token 用量。模型节点的输入包含 Context Builder 注入的参考图、候选预览和任务材料; 工具节点记录 GLSL、候选 ID 和渲染结果或编译错误。追踪会上传这些内容。

追踪元数据包含 `task_id`、`target_version`、`run_dir`、渲染尺寸、时间和尝试预算。`session_id` 对应本地 `run-*` 目录名; 如果模型只回复文本导致再次调用 Agent, 多条追踪会归入同一个 LangSmith Thread。本地 `run.json` 继续保存最终停止原因和候选选择。独立调用 `WebGL2Renderer` 不产生 Agent 追踪。

非默认区域的账户需要设置对应的 `LANGSMITH_ENDPOINT`; 多工作区密钥可能还需 `LANGSMITH_WORKSPACE_ID`。设置 `LANGSMITH_TRACING=false` 可关闭原生环境追踪。未配置时生成流程仍可正常运行。配置方式见 [LangSmith 官方文档](https://docs.langchain.com/langsmith/trace-with-langchain)。

## 运行

安装后的命令:

```sh
uv run --no-sync --env-file .env shader-deep /absolute/path/reference.png "根据参考图生成 Shader" > output.glsl
```

调整固定渲染条件、尝试上限和运行目录:

```sh
uv run --no-sync --env-file .env shader-deep /absolute/path/reference.png "根据参考图生成 Shader" \
  --width 512 --height 512 --time 0 --max-attempts 3 --output-dir runs > output.glsl
```

原来的脚本入口继续可用:

```sh
uv run --no-sync --env-file .env python main.py /absolute/path/reference.png "根据参考图生成 Shader" > output.glsl
```

查看帮助无需模型配置:

```sh
uv run --no-sync shader-deep --help
```

PNG 字节保持原样, 任务说明与实际加载的图像一起进入多模态任务消息。模型连接沿用原来的 Chat Completions 配置。标准错误输出运行目录和最终预览位置; 标准输出只包含选定候选文件中的实际代码。

退出码: 完成并选定已渲染候选时为 `0`; 预算耗尽未完成时为 `1`, 不输出未经确认的 GLSL; 输入、配置或文件错误为 `2`。模型服务异常会向调用方抛出, 已开始运行的记录保存在运行目录中。

## 独立多视角分析

在本目录执行 `make sync` 注册新命令, 然后沿用现有 `.env`:

```sh
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析构图、元素组织和可能机制" > analysis.json
```

模块入口也可直接运行:

```sh
uv run --no-sync --env-file .env python -m shader_deep.analysis_cli /absolute/path/reference.png "分析参考图"
```

程序固定本次参考 PNG 字节, 为每个子任务创建独立 Deep Agent 和图像上下文。主 Agent 使用 `run_analysis_batch` 选择预置视角或创建新视角配置, 在批次间通过 `measure_reference` 统一取证, 用 `finish_analysis` 提交综合结果; 子 Agent 只使用 `submit_analysis_report`, 可在报告的 `evidence_requests` 中提出取证需求。视角不能自行开放测量、代码、文件操作或递归委派工具。

首轮至少两个互补视角, 通常选择 2–3 个, 不读取其他视角报告或测量。主 Agent 收到一批报告后检查覆盖、关键判断和取证需求, 按重要性合并测量。追加任务声明 `purpose="supplement"` 或 `"verify"`, 填写具体缺口 `gap` 和预期新依据 `expected_evidence`, 通过 `related_result_ids`、`evidence_ids` 显式选择材料。单图无法判定的机制可作为未知项正常交付; 无需为了用满预算继续派发。

测量支持原图像素区域裁剪、区域 RGB/编码亮度均值、水平/垂直亮度剖面。区域使用左上原点的 `left/top/right/bottom`, 右、下边界排除。相同操作、区域和有效参数在同一冻结图像会话内复用同一个证据 ID, 不重复扣测量额度; 不同区域不会被强行合并。程序记录原图 SHA-256、方法版本、坐标及结果, 子 Agent 只能读取明确选入的证据。裁剪图通过真实图像内容进入上下文, 并非仅提供文件名。

工具返回简短回执, 完整报告和测量值统一通过下一轮 Context Builder 提供, 避免在历史工具消息中反复复制。分析模型默认使用 SSE 传输, 仍等待完整响应及正常结束标记后再校验和执行工具; 子任务与测量的批次时序保持同步。流中断或缺少结束标记时有限重试, 不执行半截工具参数。

超过 128 点的剖面在模型上下文中显示 `profile_digest`: 带原图坐标的均匀采样点与明暗峰谷候选。候选必须为半径 3 像素内的实际极值, 且相对左右两侧邻点的方向差均达到编码亮度阈值 1; 每种最多保留 16 个并注明截断情况。`contrast` 是双侧方向差的较小值, `neighbor_level` 是选用的保守邻点值, 不能当作相对未遮挡背景的亮度差。候选不自动等同于网格线。完整逐点数组保存在证据记录中, 窄范围测量可再次检查局部值。主 Agent 无需手工数长数组的索引。

观察和综合条目的 `basis` 默认为 `visual`; 引用数值证据时可标为 `measurement_supported` 并填写 `evidence_ids`。裁剪放大仍是目测。测量只描述选定区域, 不自动证明取样位置正确、整体视觉结论正确或唯一物理成因。透明像素在统计时合成到白底; 编码亮度使用 `0.2126R + 0.7152G + 0.0722B`, 不等于线性物理亮度。

每次模型调用将最新图像与任务资料合并到当前用户消息; 若前面是完整的 assistant/tool 交互, 则在其后追加本轮多模态消息。这样既保留工具调用与反馈配对, 也避免连续用户消息。在当前 DeepSeek 中转通道的真实对照中, 连续用户消息虽然在请求与追踪里包含图片, 模型仍无法识图; 合并后恢复了主要视觉特征识别。该兼容性结论来自本地实测, 不代表所有提供方都具有同一限制。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--max-tasks` | 6 | 累计分析子任务数量, 包括失败和补充分析 |
| `--max-parallel` | 3 | 同时执行的子 Agent 上限 |
| `--max-worker-calls` | 3 | 每个子 Agent 的模型调用上限 |
| `--max-main-calls` | 8 | 主 Agent 规划、修正和综合的模型调用总上限 |
| `--max-measurements` | 8 | 本次分析唯一测量操作上限, 缓存复用不扣额度 |
| `--max-request-retries` | 2 | 每次逻辑模型调用额外允许的瞬时请求重试次数 |
| `--request-timeout-seconds` | 120 | 每次请求尝试的网络超时, 不代表整个分析的墙钟截止时间 |
| `--stream-model-responses` / `--no-stream-model-responses` | 开启 | 模型传输使用 SSE; 收齐响应并校验后才执行工具 |
| `--max-output-tokens` | 16384 | 向提供方声明的单次请求输出预算 |
| `--output-dir` | `runs` | 独立 `run-*` 目录的父目录 |

DeepSeek 模型通过 `max_tokens` 传递输出预算。当前中转接口的 128-token 对照发现 SDK 默认生成的 `max_completion_tokens` 被忽略，因此在分析客户端中做显式映射；其他模型沿用 SDK 参数方式。该设置和 SSE 仅影响分析客户端，既有 `.env` 和生成 Agent 的配置保持原样。

格式错误、引用修正和纯文本回复都消耗逻辑调用次数。分析角色关闭自动模型摘要和客户端内部 HTTP 重试, 由分析执行器对可恢复的连接、超时、限流和服务端错误作有限重试。每次传输尝试记录耗时、错误类型和异常链类型, 与 `model_calls` 分开统计; 重试保持任务 ID, 不重复执行工具或消耗新视角名额。认证、配置及已识别的本地客户端错误不重试。最大传输尝试次数受逻辑调用上限与重试上限共同约束; 当前没有 token 总额或整个分析的硬墙钟截止时间。到限保留已有报告并明确返回未完成状态。

`run-*/reference.png` 保存固定参考, `run-*/run.json` 保存全部视角配置快照、父子任务、原始报告、综合结果、请求尝试与失败记录。`blackboard.measurements` 保存程序证据, `measurement_calls` 记录调用及缓存命中, `evidence/` 保存局部图像。并行结果由执行器集中登记, 原报告在补充分析后仍保留。快照不是可恢复完整模型会话的 checkpoint。

标准输出为 JSON, 包含 `status`、`run_dir` 和实际 `ResultRecord`（没有综合结果时为 `null`）。`completed` 退出码为 0; 子任务缺失但已有综合结果时为 `partial`, 主 Agent 到限为 `model_limit`, 运行失败为 `error`, 三者退出码为 1; 输入错误为 2。未确定的机制可以出现在正常完成的报告中。所有子任务均失败时没有可综合的证据, 不会伪造报告。

Python 入口为 `shader_deep.agents.analysis.run_analysis(path, prompt, options=...)`。已有黑板可登记一条全新的根分析任务, 再调用 `run_analysis_task(state, task_id, asset_root=..., options=...)`。选项类型在 `shader_deep.analysis.types.AnalysisOptions`。两者都是同步接口, 内部使用受限线程池并行执行子任务。

启用既有 LangSmith 配置时, 主分析追踪名为 `shader-deep.analysis`, 子分析为 `shader-deep.analysis.lens`; `session_id` 关联本次运行, `task_id`、`parent_task_id` 标明归属。图像、任务与报告会随模型和工具输入进入追踪。

Pydantic 用于工具入参和报告校验, 使用其[可校验 dataclass](https://docs.pydantic.dev/latest/concepts/dataclasses/)保留不可变记录与标准库 `asdict` 序列化。测量复用已锁定的 Pillow PNG 解码器, 从测试依赖提升为运行时依赖; 显式声明 OpenAI 已使用的 httpx2, 用于识别 SSE 中途直接抛出的传输异常。没有新增已安装包或升级现有第三方包。

详细结构和源码导航见[多视角分析实现](../../documents/png-to-shader/多视角分析.md)。

## 单 Agent 生成闭环

本轮只开放并允许执行两个工具:

| 工具 | 行为 |
| --- | --- |
| `render_shader(glsl_code)` | 保存本次代码, 使用固定 width、height、time 进行真实 WebGL2 编译和渲染, 登记候选与成功或失败结果 |
| `finish_shader(candidate_id, assessment)` | 选择本轮成功渲染且预览已进入后续模型上下文的候选, 保存自检说明并结束 |

编译错误以工具反馈和结果记录返回。渲染成功后, 工具返回候选 ID, 下一次 Context Builder 会实际加载该 PNG, 与参考、基线、代码和相关结果一起交给模型。图片通过多模态任务消息提供, 不仅返回文件路径。

结束工具不能选择未渲染的代码, 也不能在首次渲染的同一批工具调用中跳过预览检查。完成后直接读取已选候选文件作为最终代码, 无需模型再生成一份可能不同的代码。模型自检和选择不代表用户已接受视觉效果。

默认最多渲染 3 个候选, 编译失败也计数。模型调用轮数上限为 `max_attempts + 2`, 不含客户端内部网络重试。即使模型反复请求渲染或只返回普通文字, 运行也会到限停止, 保留已有文件与停止原因。

浏览器在一个专用执行线程中创建、使用和关闭。多个工具请求经过该线程串行处理, 浏览器可跨尝试复用, 尺寸、时间和尝试次数由代码控制。

每次在 `--output-dir` 下创建独立的 `run-*` 子目录:

```text
run-*/
├── candidate-001.glsl      # 第一次尝试, 失败代码也保留
├── candidate-002.glsl
├── candidate-002.png       # 仅成功渲染的尝试有 PNG
└── run.json                # 固定条件、计数、选择、停止原因和黑板快照
```

`run.json` 的 `selected_candidate_id` 在未完成时为空, `stop_reason` 记录 `completed`、`attempt_limit`、`model_limit` 或 `error`。快照更新采用临时文件替换, 不保存模型连接配置或 API 密钥。

## 当前目录

```text
shader_deep/
├── main.py                  # 兼容原脚本启动和导入名称
├── pyproject.toml           # 依赖、打包和 shader-deep 命令入口
├── uv.lock
├── Makefile
├── .env.example
├── src/shader_deep/
│   ├── __init__.py
│   ├── cli.py               # 参数解析、标准输出与退出码
│   ├── analysis_cli.py      # 独立多视角分析命令和 JSON 输出
│   ├── analysis/           # 视角、报告类型、验证、角色循环与并行执行
│   ├── config.py            # 环境变量和模型创建
│   ├── schemas.py           # 四类业务记录、黑板状态和任务材料
│   ├── blackboard.py        # 登记、引用校验和按任务读取
│   ├── context/             # 按 Agent 组织上下文构造
│   │   ├── __init__.py      # 统一公开导入入口
│   │   ├── generation.py   # 生成任务选材、代码加载和材料清单
│   │   ├── analysis.py     # 主分析和独立视角的图像与证据选材
│   │   └── common.py       # 共用 PNG 读取与 data URL 转换
│   ├── middleware.py        # 每轮模型调用前刷新任务上下文
│   ├── tools/
│   │   ├── __init__.py      # 统一导出 RenderSession、GenerationOutcome、GenerationLimitError
│   │   ├── session.py       # 工具绑定、串行线程、浏览器生命周期与运行快照
│   │   ├── render.py        # 渲染候选、保存 GLSL/PNG、登记成功或失败结果
│   │   ├── finish.py        # 检查候选选择条件、保存自检结论并结束
│   │   └── types.py         # 运行结果数据类型与预算异常
│   ├── artifacts.py         # 唯一运行目录和业务快照保存
│   ├── rendering/
│   │   ├── __init__.py      # WebGL2Renderer、RenderError
│   │   ├── renderer.py      # Python API、浏览器生命周期、PNG 字节
│   │   └── webgl2.js        # WebGL2 编译、链接、绘制和 PNG 导出
│   └── agents/
│       ├── __init__.py
│       ├── analysis.py      # 独立多视角分析公开入口
│       └── generation.py    # 有预算的生成、渲染和选择循环
└── tests/
    ├── unit_tests/          # 业务规则、Context Builder、模型模拟和渲染入参
    └── integration_tests/
        ├── test_renderer.py # 真实 Chromium WebGL2 像素验收
        └── test_generation.py # 模拟模型 + 真实编译修复与预览反馈
```

## 独立 WebGL2 渲染

`shader_deep.rendering` 仅依赖 Playwright 和标准库, 不导入 Agent、黑板或 LangChain。使用同步接口, 在同一线程内串行复用一个实例; 不在已运行的 asyncio 循环中直接调用这个同步接口:

```python
from pathlib import Path

from shader_deep.rendering import WebGL2Renderer

code = """
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec2 uv = fragCoord / iResolution.xy;
    fragColor = vec4(uv, 0.5 + 0.5 * sin(iTime), 1.0);
}
"""

with WebGL2Renderer() as renderer:
    png = renderer.render(glsl_code=code, width=512, height=320, time=0.0)
    Path("/absolute/path/render.png").write_bytes(png)
```

| 输入或输出 | 约定 |
| --- | --- |
| `glsl_code` | GLSL ES 3.00 的 `mainImage(out vec4, in vec2)` 及辅助代码, 单 Pass、无外部纹理 |
| `width`、`height` | 正整数, 对应 PNG 的实际像素尺寸; 超出 WebGL2 能力时返回错误 |
| `time` | 有限秒数, 直接赋给 `iTime`, 每次只绘制指定时刻的一帧 |
| 返回值 | PNG `bytes`, 保留 Shader 输出的 alpha; 保存位置由调用方决定 |

模块提供 `#version 300 es`、默认 highp precision、`iResolution = vec3(width, height, 1)`、`iTime` 和调用 `mainImage` 的 `main()`。输入不要重复声明这些 uniform、版本或 `main` 入口。坐标沿用 ShaderToy 的左下原点, Canvas 直接导出正常方向的 PNG; 不使用页面截图, 不额外做图像缩放或色调映射。

`with` 进入时创建临时配置的独立无头浏览器, 退出时关闭浏览器与驱动。每次渲染重新编译、绘制并释放 program 和 Shader, 不保留前一帧作为输入。默认 `channel="chromium"` 使用配套浏览器, 如需使用本机 Chrome 可显式指定 `WebGL2Renderer(channel="chrome")`。

无效入参抛出 `ValueError`。浏览器、WebGL2 或 Shader 执行错误抛出 `RenderError`, 可读取 `stage` 和 `log`:

```python
from shader_deep.rendering import RenderError

with WebGL2Renderer() as renderer:
    try:
        png = renderer.render(code, 512, 320, 0.0)
    except RenderError as error:
        print(error.stage, error.log)
```

常见阶段包括 `init`、`fragment_compile`、`link`、`size` 和 `draw`。GLSL 编译失败保留驱动日志, 用户代码从第 1 行计数。编译或链接失败后可以继续使用同一实例; 画面全黑本身不代表执行失败。

## 业务数据与黑板

`schemas.py` 使用标准库的冻结数据类定义记录, 黑板状态是由调用方持有的普通字典。`blackboard.py` 的更新函数返回新状态, 调用方需要接收返回值; 函数不会修改传入状态或覆盖已有记录。

| 接口 | 行为 |
| --- | --- |
| `new_blackboard()` | 创建一个项目的空黑板 |
| `add_target(state, target)` | 登记新目标版本, 不改变已有任务的目标绑定 |
| `add_task(state, task)` | 核对目标、基线、指定候选与历史结果引用后登记任务 |
| `add_candidate(state, candidate)` | 登记生成任务的候选, 来源目标和基线通过 `task_id` 追溯 |
| `add_result(state, result)` | 登记任务结果, 允许引用指定候选输入、显式关联历史结果的候选证据和本任务产出 |
| `read_task(state, task_id)` | 返回绑定目标、基线、指定输入、本任务产出及明确关联的结果 |

最小用法:

```python
from shader_deep.blackboard import add_target, add_task, new_blackboard, read_task
from shader_deep.schemas import TargetRecord, TaskRecord

state = new_blackboard()
state = add_target(
    state,
    TargetRecord(version="T1", request="复刻参考图", reference_path="inputs/reference.png"),
)
state = add_task(
    state,
    TaskRecord(id="G1", role="generation", target_version="T1", objective="生成整图粗稿"),
)
records = read_task(state, "G1")
```

每个目标版本、任务、候选和结果标识在各自集合中唯一。任务的绑定内容在登记后保持固定; 新目标下可以创建新的任务, 显式引用旧候选进行重新评价, 同时保留候选的原始来源。

结果中的观察、假设、限制和建议分别保存。部分完成与后续完成可以登记为不同结果记录。登记结果不会自动采用候选, 已有任务的基线继续保持原值。

黑板接口只处理 Python 业务数据与引用, 不读取或写入制品文件, 不验证代码或预览有效性, 也不解析模型返回的 JSON。`read_task` 返回业务记录, `context/generation.py` 在此基础上加载生成所需的材料; 共用的 PNG 读取放在 `context/common.py`。

源码职责、调用链、后续多 Agent 扩展建议和本轮验证见[源码结构与审查](../../documents/png-to-shader/源码结构与审查-2026-09-11.md)。

## 生成任务的 Context Builder

`build_generation_context(state, task_id, asset_root=...)` 只处理生成角色。它加载本任务绑定的参考图、基线代码与已有预览、明确指定的候选输入和本任务已登记产出, 加入显式关联的历史结果及本任务结果。其他任务的记录不会因为位于同一黑板就自动进入上下文。

任务信息包含目标版本、基线、保护项、修改范围、假设和结束条件。候选及历史结果同时标明原始目标和基线, 观察与假设保留在不同字段中。`GenerationContext` 返回实际的多模态消息, 以及候选 ID、结果 ID 和已读取文件的清单。

相对文件路径按 `asset_root` 解析, 默认使用当前工作目录。指定了代码或预览路径但文件不可用时会返回错误; `preview_path=None` 明确表示没有提供预览。首版支持 PNG, 不缩放或裁剪图像。

例如, 在上面的黑板示例基础上:

```python
from pathlib import Path

from shader_deep.agents.generation import run_generation
from shader_deep.config import GenerationOptions
from shader_deep.context import build_generation_context

asset_root = Path("/absolute/path/to/run")
context = build_generation_context(state, "G1", asset_root=asset_root)
print(context.files)  # 只检查材料清单, 无需模型配置或模型请求

# 需要实际模型配置和配套浏览器, 返回候选、最新黑板和制品位置.
outcome = run_generation(
    state, "G1", asset_root=asset_root,
    options=GenerationOptions(width=512, height=512, max_attempts=3),
)
print(outcome.stop_reason, outcome.run_dir)
if outcome.selected_candidate is not None:
    print(outcome.selected_candidate.code_path, outcome.selected_candidate.preview_path)
```

`run_generation` 从传入黑板开始, 将每次工具产生的新状态提供给下一轮 Context Builder, 并在 `GenerationOutcome.state` 中返回更新后的黑板; 调用方的原始状态保持不变。`run_shader(path, prompt, options=...)` 可直接从 PNG 开始并返回同样的结果。

原来的 `generate_shader(path, prompt)` 和 `generate_task(state, task_id, asset_root=...)` 仍返回字符串, 可额外传入 `options=GenerationOptions(...)`。它们现在只返回已渲染并选定的代码; 未完成时抛出 `GenerationIncompleteError`, 可通过异常的 `outcome` 获取已有文件和停止原因。

任务材料中间件使用 `request.override(messages=...)` 只改变本次请求, 不把临时材料消息追加到聊天状态。框架提供的有效历史与最新工具结果保留。生成循环中间件另外限制可执行工具、检查预算并处理候选选择。材料构造支持同步和异步调用, 当前完整生成运行入口为同步接口。

提示词要求先渲染, 再对照真实预览进行修正或结束。候选选择只结束当前生成任务, 不修改原任务的基线, 也不自动建立全局采用或用户接受记录。

生成角色的选材与每轮注入继续使用上述入口。独立分析的 Builder 在 `context/analysis.py`; 评审角色、领域 token 预算、材料截断策略及自定义历史摘要尚未实现。生成角色的聊天历史管理沿用 Deep Agents; 独立分析采用有次数预算的完整任务历史。

## 开发检查

日常开发检查在安装后运行, 以下命令不会启动真实浏览器:

```sh
make test
make lint
```

测试使用标准库 unittest, 将警告视为错误。Agent 链路测试在 HTTP 传输处提供模拟模型响应, 不读取实际 `.env`、不访问外部模型, 也不产生模型费用。

修改渲染器、浏览器生命周期或图像反馈链路后, 再运行真实浏览器检查; 纯注释、文档和目录调整通常只需上面的检查:

```sh
make integration_test
```

`make integration_test` 先单独运行现有的纯色渲染测试, 确认浏览器和 WebGL2 可用后再运行整组测试。前置检查失败时不会进入整组测试, 整组测试也会在首个失败时停止。这样避免浏览器无法启动时, 多个测试连续触发崩溃弹窗; 它不能消除第一次启动失败本身。

在 Codex 等受限执行环境中, 真实浏览器检查应使用已获批准且允许启动本地 Chromium 的执行权限。若出现 `kill EPERM`、`SIGABRT` 或浏览器初始化失败, 先检查执行权限, 不在同一失败环境里重复运行。`headless=True` 只隐藏浏览器窗口, 不阻止系统崩溃提示; 不要通过关闭系统崩溃提示或设置 `chromium_sandbox=False` 处理本次权限问题。

集成测试使用真实 Chromium 执行 Shader, 再用 Pillow 解码 PNG, 检查颜色、尺寸、坐标方向、alpha、时间、编译和链接错误、连续渲染及生命周期。生成闭环测试使用模拟模型响应, 但实际执行浏览器编译、错误修复后的渲染和 PNG 反馈。缺少浏览器时会失败, 先执行 `make browsers`。

这些测试不验证具体外部模型服务的联网可用性或模型的实际视觉判断能力; 真实模型联调需使用配置的服务另行执行。格式调整使用 `make format`。检查命令使用已安装环境, 不隐式安装依赖。
