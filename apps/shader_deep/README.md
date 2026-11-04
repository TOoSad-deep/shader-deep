# Shader Deep

维护入口: [当前架构](docs/architecture.md) · [设计决策](docs/decisions/0001-agent-oriented-layout.md) · [重构进度与验证](docs/work-items/structure-refactor.md) · [开发检查](docs/development.md).

输入本地 PNG 和文字要求, 通过单个 Deep Agent 生成、渲染、查看预览并修正 Shader, 将选定候选的实际 GLSL 写入标准输出。

当前已连接内存黑板、Context Builder、真实 WebGL2 渲染工具和有预算的生成循环。每次尝试保存代码、预览或错误, 完成时明确选择已渲染且已向模型展示预览的候选。

独立分析提供 `shader-deep-analyze` 命令: 主 Agent 确定一个目标元素与范围, 默认三个独立探索 worker 产出特征、关系、机制和草图, 异步整合 worker 仅合并 F/R/M. 程序维护引用并交付 `five_libraries_v1` 完整文件包、短入口及按草图读取视图. 分析到生成的自动交接尚未接入, 候选仍需编码、渲染与用户视觉验收.

## 独立调参实验

[实时预览页面](src/shader_deep/rendering/preview.html)可直接用浏览器打开. 页面持续绘制, 滑块只更新 uniform, 不重新编译 Shader. Python 调用方式及参数、快照、线程和错误契约见[渲染模块 README](src/shader_deep/rendering/README.md).

实验包含以下文件:

| 文件 | 职责 |
| --- | --- |
| [tools/](src/shader_deep/experiments/tools/README.md) | 每个模型工具单独一个文件, 由 factory.py 装配依赖 |
| [parameter_tool.py](src/shader_deep/experiments/parameter_tool.py) | 旧导入路径兼容转发, 不再包含工具实现 |
| [_parameter_history.py](src/shader_deep/experiments/_parameter_history.py) | 保存本次工具绑定的成功截图和参数, 不评分或选优 |
| [comparison.py](src/shader_deep/imaging/comparison.py) | 将等尺寸 PNG 原尺度并排, 不计算误差 |
| [parameter_agent.py](src/shader_deep/experiments/parameter_agent.py) | 使用 Deep Agents 构建可独立 invoke 的调参 Agent, 不需要主 Agent |
| [parameter_task.py](src/shader_deep/experiments/parameter_task.py) | 把只读目标 PNG 装入多模态任务消息, 不传目标参数或本地路径 |
| [parameter_run.py](src/shader_deep/experiments/parameter_run.py) | 计数实际请求尝试, 将图执行和最终取图纳入同一追踪, 失败不覆盖原异常 |

调用方指定 Shader、参数白名单及范围, 使用外层 with 管理常驻浏览器. 工具只转发参数更新和取图, 不启动/关闭浏览器、不保存候选文件、不重复实现参数范围校验. 取图响应包含完整当前参数、版本、时间、尺寸及实际 PNG 图片内容.

以下代码可保存为 `parameter_example.py`. 只验证工具时, 执行到 `blocks = ...` 即可, 不执行后面的 Agent 部分, 此时不需要模型配置:

```python
from langchain_core.messages import HumanMessage

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_run import run_parameter_task
from shader_deep.experiments.tools import build_parameter_tools
from shader_deep.rendering import ShaderPreview

shader = """
uniform float RED;
uniform float GREEN;
void mainImage(out vec4 color, in vec2 pixel) {
    color = vec4(RED, GREEN, 0.0, 1.0);
}
"""
with ShaderPreview(
    shader,
    parameters={
        "RED": {"min": 0.0, "max": 1.0, "initial": 0.2},
        "GREEN": {"min": 0.0, "max": 1.0, "initial": 0.3},
    },
) as preview:
    tools = {operation.name: operation for operation in build_parameter_tools(preview)}
    tools["set_parameters"].invoke({"parameters": {"RED": 0.8}})
    blocks = tools["capture_preview"].invoke({})

    agent = build_parameter_agent(preview=preview)
    result = run_parameter_task(
        agent,
        HumanMessage(content="观察当前图片, 降低红色强度, 查看调整后的图片并说明结果"),
        preview,
        config={"recursion_limit": 20},
    )
    print(result.messages[-1].content)
```

真实模型调用在应用目录执行 `uv run --no-sync --env-file .env python parameter_example.py`, 配置规则见下文. 构建函数还允许通过可选 `model=` 传入已有模型, 便于离线测试. 该示例中的图步数上限不等于调参次数预算.

执行保护: `build_parameter_agent` 保留原签名, OpenAI 兼容模型缺省单次输出为 8192 tokens、超时 45 秒, 显式预算/超时继续生效. 标准 OpenAI SDK 客户端关闭内部重试后, 原生 middleware 只对建连错误等待一秒、额外重试一次; 读取超时、认证及配置错误直接抛出. 重试只覆盖模型请求, 不重放成功工具. `finish_reason=length` 在工具执行前抛出 `ParameterOutputError`, 不把截断正文或工具参数当作成功结果. Chat Completions 流式模式在片段合并前检查结束标记, 去重一致的结束帧, 拒绝缺失/冲突/截断终态及不完整的工具参数 JSON, 同步和异步入口使用相同规则.

支持通过 ChatOpenAI 的 `client` / `async_client` 直接注入标准 completions 客户端; 即使 `root_client` / `root_async_client` 为空, 仍从已有 SDK owner 复制配置并共享原传输连接, 不修改或关闭调用方客户端. 直接注入资源保留其已有超时, 除非模型显式设置了请求超时. 调用方 ChatOpenAI 子类的覆盖方法继续生效. 无法识别 SDK owner 的自定义 completions 对象保留原样, 其内部重试和超时由调用方管理, 本模块不承诺控制其内部 HTTP 请求数量.

发现流协议错误后, 后续片段只被消费而不再交给消息聚合, 待 SDK 完成本次响应清理后抛出首个错误; 后续读取/关闭故障作为附加说明. 这可能等待当前响应结束或超时, 不等于即时断流. 外部取消/生成器关闭继续向外传播, 不转成继续排空响应.

推荐 `run_parameter_task(agent, task, preview, max_model_calls=20, config=None)` 执行完整实验. 它按实际模型请求尝试计数, 失败和重试都占预算; LangSmith 根名称为 `shader-deep.parameter-run`, 包含最终取图失败. 仅图成功后取最终快照, 模型/工具失败时直接保留原异常; 不在 `finally` 无条件取图. 直接 `agent.invoke/ainvoke/stream` 仍可使用, 但调用方需自行管理总预算与收尾. 成功返回 `ParameterRunResult` 不代表视觉匹配已通过验收.

上述请求预算覆盖 LangChain 可见的请求尝试; 若传入不透明的自定义客户端, 需要调用方另行约束客户端内部的请求或重试.

工具参数/范围错误仍返回可纠正的错误消息. 浏览器、会话、绘制或导出故障直接抛出 `RenderError` 终止任务. 本版采用停止策略, 不自动重建浏览器或复用旧图假装恢复; 若调用方显式新建会话, 应重新绑定工具/历史并检查新截图.

更新只合并指定参数, 未提供的参数保留当前值. Agent 应先取基线图, 分轮执行更新和取图, 并根据取图结果观察. 更新回执不代表已看到图片; 同一轮并行更新和取图不保证图片属于刚提交的新值. 并行比较多个候选应使用独立的预览会话.

迁移说明: 旧实验工厂 `build_render_parameters_tool(shader_path, parameters, options=...)` 已替换为 `build_parameter_tools(preview)`, 不再提供旧 render_parameters 工具、隐式候选文件或三次尝试额度. `build_parameter_agent` 新增可选 `preview=` 和 `model=`, 原 `render_tool=` 单工具参数保留兼容, 不能与 preview 同时提供. 真实模型调参效果仍需单独验证, 模拟模型图执行及 PNG 像素测试不代表视觉质量已验收.

### 按参考图匹配

文字调参实验和参考图匹配是两种不同任务. 通过 `build_reference_task(reference_path)` 传入真实目标图片, 并用 `build_parameter_agent(preview=preview, reference_path=reference_path)` 绑定同一参考图. 模型优先使用 `compare_preview` 的左参考、右当前并排图, 根据可见差异定向调整. 不在任务中告诉模型“减弱一半”等参数变化答案.

以下完整示例先用同一 Shader 生成可达的参考图, 目标参数只存在于调用方代码中; 模型消息只包含 PNG 内容及任务说明. 可保存为 `parameter_reference_example.py`, 然后通过既有模型配置执行 `uv run --no-sync --env-file .env python parameter_reference_example.py`:

```python
from pathlib import Path

from shader_deep.experiments.parameter_agent import build_parameter_agent
from shader_deep.experiments.parameter_task import build_reference_task
from shader_deep.experiments.parameter_run import run_parameter_task
from shader_deep.rendering import ShaderPreview, WebGL2Renderer

shader = """
uniform float RED;
void mainImage(out vec4 color, in vec2 pixel) {
    color = vec4(RED, 0.3, 0.0, 1.0);
}
"""
reference = Path("reference.png")
with WebGL2Renderer() as renderer:
    # 隐藏目标值只用于生成图片, 不传入 agent 或工具说明.
    reference.write_bytes(renderer.render(shader, 512, 512, 0.0, uniforms={"RED": 0.65}))

task = build_reference_task(reference)
with ShaderPreview(
    shader,
    parameters={"RED": {"min": 0.0, "max": 1.0, "initial": 0.2}},
    width=512,
    height=512,
    time=0.0,
) as preview:
    agent = build_parameter_agent(preview=preview, reference_path=reference)
    result = run_parameter_task(agent, task, preview, config={"recursion_limit": 20})
    final = result.snapshot
    Path("matched.png").write_bytes(final.png)
    print(dict(final.parameters))  # 实际提交的参数以会话状态为准.
```

`build_reference_task` 返回 `HumanMessage`, 可选 `requirement=` 添加匹配优先级等补充说明. 构建时读取并固定 PNG 字节, 后续修改原文件不改变已构建的消息. 路径和文件名不发送给模型; 消息读取沿用文件存在/扩展名检查, 参考工具绑定时另验证 PNG 可以解码. 消息和工具分别固定参考图, 应连续构建且中间不修改文件; 换任务/参考图时重新构建 Agent, 不复用旧历史. 生成参考图和当前预览时应使用相同尺寸及固定时间. 不同工具配置、预算和提示词的真实实验分别记录, 既往结果不能直接证明最新配置提高了精度; 具体证据见下方实验记录.

#### 差异、参数与验证流程

调参 Agent 的提示词要求每轮按 `差异 → 参数 → 修改后验证` 操作, 只需简短说明观察与决定:

1. 先观察基线, 指出一至三项主要差异. 默认先处理轮廓、整体大小和位置, 再处理颜色与光晕; 用户指定的重点优先.
2. 每次更新前说明要解决的差异、所选参数及预期可见变化, 再调用 `set_parameters`.
3. 成功更新后单独取图, 核对完整参数及 `revision`, 判断该差异是改善、无明显变化、变差还是无法判断, 然后决定下一次调整.
4. 结束前用最后一次修改后的最新截图逐项复查主要差异, 报告最终参数、截图 `revision` 和剩余问题. 恢复旧值也需检查恢复后的新截图. 若最新图已检查且没有后续修改, 不要求重复取图.

上述视觉检查流程是模型可见的提示词约束, 尚未新增结束提交工具或程序验证视觉判断; 不保证模型必然遵循或判断正确. 执行层另行拦截输出截断及致命工具错误. 预算用尽或观察受限时, 要求模型如实报告尚未匹配/未完成验证. 历史回读保持可选, 工具不提供数值评分. 此版已完成双圆 case 的单轮 A/B 实测, 模型执行了逐轮与结束前检查, 但尚未证明精度稳定提高; 具体结果见下方实验记录.

#### 只读视觉反馈工具

`build_parameter_tools(preview, reference_path=reference)` 返回四个工具; 不提供参考图时仍只返回原来的两个, 名称和顺序不变. Agent 构建函数只新增可选关键字参数 `reference_path=None`, 需要同时提供 `preview`.

| 工具 | 输入 | 输出与作用 |
| --- | --- | --- |
| `set_parameters` | `parameters` 字典 | 校验并更新指定参数, 返回完整参数; 不截图、不评分 |
| `capture_preview` | 无 | 原始当前 PNG 和元数据; 绑定参考图时另记录截图编号 |
| `compare_preview` | 无 | 捕获当前帧, 左参考右当前并排图及当前参数、截图编号; 不做数值评价 |
| `get_trial_history` | 可选 `trial_ids` | 默认最近三张; 可指定一至三个不同编号, 返回原始截图和参数 |

两张 `512×512` PNG 拼成 `1024×536` RGBA PNG, 顶部 24 像素标题栏不覆盖原图. 源图不裁剪、不缩放, RGBA 像素保持一致, 源 PNG 字节保存在历史快照中. 尺寸不同时返回错误, 不静默缩放. 对比响应中的 `width/height` 是当前源图尺寸, `image_width/image_height` 是并排图尺寸, `layout=reference_left_current_right` 标注左右身份.

`trial_id` 从 1 开始, 表示本次绑定中成功取图的记录顺序, 包括基线、重复参数截图及 `capture_preview`; 它不是 `revision` 参数版本. 仅更新但未截图的状态没有历史图, 失败的截图/对比不新增编号. 历史默认按时间顺序展示, 不自动排名、选择或恢复参数; 模型需自行看图并用 `set_parameters` 恢复某次值. 每次回读最多三张以控制图片输入量, 可再次指定更早编号. 全部成功截图暂存本次工具闭包内存, 不持久写盘, 构建新工具时历史为空; 运行长度由外层限制.

工具不计算或返回 MAE、其他相似度评分、差异图、优劣标签及自动最好结果. 外层离线评估可以保留 MAE 作为指标之一, 但不要把评估文件或结果注入模型消息、工具描述或可读取文件. 局部放大尚未实现. 更新和观察继续分轮执行, 并排图说明的是当前 capture 的真实状态, 不把更新回执当作视觉证据.

当前范围及验证结果见[实验记录](docs/work-items/parameter-agent-experiment.md).

## 安装与配置

在本目录操作, 使用仓库约定的 `uv` 管理独立环境:

```sh
cd /Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/apps/shader_deep
make sync
```

应用以可编辑包安装到本目录的 `.venv`。`deepagents` 继续使用 `../../libs/deepagents` 的本地可编辑依赖。应用直接声明 LangChain 依赖以使用其消息和 middleware 接口, 版本范围与当前 SDK 一致。渲染模块使用 Microsoft 维护的 Playwright 及其配套 Chromium; Pillow 用于参考图测量、裁剪及测试中的像素核对。构建采用 setuptools, Ruff 和 ty 仅作为开发检查工具。

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

配置优先级为 `OPENROUTER_*` → `DS_MICU_*` → `MICU_*`。一组中任一项非空即整组选用, 缺少任一项会直接报错, 不会跨组借用密钥或地址; 全部为空白的占位组会跳过。

使用 OpenRouter 时填写 `OPENROUTER_MODEL`、`OPENROUTER_BASE_URL` 和 `OPENROUTER_API_KEY`, 地址为 `https://openrouter.ai/api/v1`。模型须支持图像输入及工具调用, ID 以 [OpenRouter 模型目录](https://openrouter.ai/models)为准。客户端使用 Chat Completions 并要求提供方支持实际请求参数; 分析适配层发送网关 `max_tokens`, 并保留、汇集和回传工具调用历史中的 `reasoning_details`。流式和非流式请求均保留原始工具参数的执行前检查。

可选 `OPENROUTER_REASONING_EFFORT` 显式控制推理强度, 未配置时沿用模型默认; 所选值必须受模型支持, 必须推理的模型不能设为 `none`。OpenRouter 重复的相同 SSE 结束标记会去重, 冲突或缺失标记仍被拒绝, 不降低工具执行的完整性检查。

可选 `OPENROUTER_PROVIDER_SORT` 使用 `price`、`throughput` 或 `latency` 优先选路, 未配置时沿用网关默认。分析请求为函数工具声明 `strict: true`, 保留原 Schema 中动态选择字典的键和值约束; 提供方返回仍须经过本地字段、引用与业务校验, 严格模式不替代这些检查。

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

## 单元素多视角分析

```sh
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "只分析中央粉色圆形本体" --output-dir runs > analysis-index.json
```

默认一批三个独立视角, `--max-tasks 2` 可以减少到两个. 第四个视角或结果返回后的新探索批次会被拒绝. 重试保持原目标、方向和输入, 不增加视角. 主 Agent 只定位元素与确定范围, 不预填特征观察或机制答案. 探索的共同材料是固定原图、用户要求、目标元素与范围, 每个 worker 使用自己的方向和模型历史.

整合使用独立线程和历史, 从不可变 V0 目录按 ID 读取正文, 只合并已有特征、关系和机制. 程序拒绝改变方案含义的映射, 草图直接汇集且仅更新引用. 合法 V0 在整合启动前持久保存; 整合或 V1 发布失败后交付 V0、`partial` 和具体缺口. 无合法可用观察基线时返回 `failed`, 不发布空包.

### 文件包与读取

CLI 标准输出只返回 JSON 状态、运行目录、`report_dir` 和短结果索引, 标准错误实时显示关键步骤和入口位置. `AnalysisOutcome.report_dir` 是新增的可选返回属性, 公开函数签名保持不变; 结果协议及交付载体已从四库切换为五库.

```text
run-*/
  reference.png
  commit.json       # 任务、回执、结果和版本指针的唯一权威发布记录
  results/          # 不可变原始产物、V0/V1; 来源与恢复信息保留在运行记录
  run.json          # 从权威记录生成的可读投影
  analysis.log      # 全级别可读日志, 包含完整业务提交与 V0/V1 正文
  events.jsonl      # 模型调用、网络尝试、用量与执行事件
  tools.jsonl       # 原有工具参数和回执审计
  report/
    README.md       # 目标、状态、草图目录、缺口和读取导航
    manifest.json   # 本轮公共 state 与格式版本
    elements.json
    features.json
    relations.json
    mechanisms.json
    sketches.json
    reference.png
```

```python
from pathlib import Path
from shader_deep.api import run_analysis, read_report_package, read_sketch
from shader_deep.workflows.configuration import load_analysis_options

outcome = run_analysis(Path("/absolute/path/reference.png"), "分析中央圆形", options=load_analysis_options())
if outcome.report_dir is not None:
    manifest, libraries = read_report_package(outcome.report_dir)
    # 草图 ID 来自目录, 没有自动排名或全局默认草图.
    if libraries.sketches:
        materials = read_sketch(outcome.report_dir, libraries.sketches[0].id)
        # 局部备选须显式选择, 不会自动叠加多个备选.
        # materials = read_sketch(outcome.report_dir, "S1", alternative=0)
```

按草图返回的是有范围的取材视图, 保留整轮状态、全部缺口和未知项. 视图提供有效选择及必要正文, 其他候选继续从完整包读取, 不声称视图是自包含小包. 完整文件包可整体移动, 原图哈希和全部引用可校验. 已发布包不可覆盖.

### 实时运行日志

默认 `--log-level INFO` 将运行开始、元素拆分、探索方向、每个子 Agent 的提交摘要、拒绝原因、请求耗时与 token 用量、版本发布和最终状态输出到 stderr. 每条记录带 UTC 时间、run/task/attempt 身份及模型调用序号, 并行结果可按任务区分. INFO 摘要会显式标记长文本截断; 完整正文另写入本轮 `analysis.log`.

```sh
# JSON 索引保存到文件, 关键进度仍显示在终端.
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析中央圆形" > analysis-index.json
# 终端也查看完整业务提交; 文件日志始终包含 DEBUG 记录.
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析中央圆形" --log-level DEBUG > analysis-index.json
```

`--log-level WARNING` 或 `ERROR` 可减少终端信息, 不减少文件日志. 后台执行时可将 stderr 重定向到控制台日志并使用 `tail -f` 查看; 也可直接查看运行目录的 `analysis.log`. Python API 始终保存文件日志, 控制台输出沿用宿主对 `shader_deep.analysis` logger 的配置.

日志区分尚未校验的模型响应、被接受或拒绝的工具提交、已发布版本. 执行成功不等于内容质量或视觉验收通过. 文本日志不记录图片 Base64、推理字段和客户端密钥; 写入故障会报告警告, 不改变业务提交和报告发布. 日志属于诊断材料, 权威恢复状态仍为 `commit.json`.

### 配置与恢复

配置文件为 [resources/analysis.yaml](src/shader_deep/resources/analysis.yaml), 按模块位置加载. 显式 CLI 参数优先于 YAML, YAML 优先于 `AnalysisOptions` 内置值; 相对 YAML 输出路径基于配置文件所在目录. `--config` 可选其他文件, 显式文件不存在或字段错误会在模型调用前报错. Python 直接调用使用 `AnalysisOptions`, 可显式调用 `load_analysis_options` 加载 YAML.

`max_main_calls` 由规划、主恢复诊断与整合共用, 并发扣费读取持久计数; `max_main_calls` / `max_worker_calls` 默认 0, 不设总模型调用限额; 提供方单次输出容量、请求超时和上下文检查继续生效. 阶段输出覆盖顺序保持: 阶段 CLI > 通用 CLI > 阶段 YAML > 通用 YAML > 内置值. `outline_max_output_tokens` 在新流程用于目标规划, `worker_max_output_tokens` 用于探索, `integration_max_output_tokens` 用于整合.

网络暂时错误默认额外重试两次, 只重试请求、不重放工具. 每个逻辑任务最多两次提交修复, 跨重派不重置; 每个任务初次执行加一次自动重派; 自动恢复失败后由主 agent 在独立受限上下文诊断, 决定最后重派原任务或结束. 恢复决策自身失败不会递归恢复, 也不能改变目标、方向或加入兄弟结论. 连续三轮没有新的有效读取或接受的提交会结束当前实例并进入恢复策略. 永久配置、认证和输入容量错误直接报告.

运行状态和结果回执一起原子发布. 完成事件只用于唤醒, 协调器等待期间补查持久状态; 相同成功提交在封存后重试仍返回旧回执. 取消、替换和封存撤销新提交权限. 每轮进程锁和运行代次阻止两个协调器同时写入; 恢复任务状态不承诺恢复旧模型推理或聊天会话.

草图最低产出要求继续另议. 当前按已表达的业务缺口计算部分完成, 不增加每个 worker 的草图数量门槛. `completed` 只说明本轮流程与已知必要工作完成, 不证明视觉效果、机制正确或上下文成本收益.

### 四库兼容与历史回放

旧 `possibility_library_v1` 实现保留于 `workflows/legacy_analysis.py`、旧角色和 `domain/library/`. 旧数据不自动转换为五库. 历史回放继续使用四库链路, 不计作新默认流程的验证:

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.replay /absolute/path/four-library-source-run --output-dir runs/replay --max-main-calls 8
```

当前实现地图见[架构说明](docs/architecture.md); 数据契约见[五库设计](docs/analysis-five-libraries.md); 角色与恢复契约见[分析架构设计](docs/analysis-architecture-design.md). 本轮实施及实际验证记录见[五库实施记录](docs/work-items/five-library-implementation.md).

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

## 当前目录与源码入口

```text
src/shader_deep/
├── api.py                  # 稳定 Python API
├── cli/                    # 分析、生成、回放命令
├── workflows/              # 调度、阶段推进、主进度与最终交付
├── agents/                 # five_analysis / generation / 四库兼容角色
├── domain/                 # 业务记录、五库契约与四库兼容规则
├── runtime/                # 调用循环、预算、历史、提交与修复
├── infrastructure/         # 模型传输、配置读取、制品存储与追踪
├── imaging/                # 图像读取、测量与剖面
├── rendering/              # 独立 WebGL2 渲染器
├── resources/              # 默认 YAML
├── compatibility/          # 显式保留的旧流程
└── experiments/            # 固定样本回放实验
```

从 [当前架构](docs/architecture.md) 阅读完整职责、调用链和状态归属; 修改与验证方法见 [开发指南](docs/development.md). `analysis/`、旧 `context/`、`tools/` 及旧模块路径是兼容转发, 新实现不从这些路径导入.

`workflows/five_analysis.py` 组合目标规划、独立探索和异步整合; `domain/five_libraries/` 集中维护字段与引用; `runtime/task_store/` 管理持久生命周期; `infrastructure/storage/report_package.py` 发布和读取文件包. 旧协调器只服务四库兼容及回放.

测试按业务和执行职责组织在 `tests/unit_tests/` 的子目录中. `tests/integration_tests/` 保留真实浏览器渲染检查.

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

黑板接口只处理 Python 业务数据与引用, 不读取或写入制品文件, 不验证代码或预览有效性, 也不解析模型返回的 JSON。`read_task` 返回业务记录, `agents/generation/context.py` 在此基础上加载生成所需的材料; 共用的 PNG 读取放在 `infrastructure/llm/messages.py`。

源码职责、调用链、后续多 Agent 扩展建议和本轮验证见[源码结构与审查](../../documents/png-to-shader/源码结构与审查-2026-09-11.md)。

## 生成任务的 Context Builder

`build_generation_context(state, task_id, asset_root=...)` 只处理生成角色。它加载本任务绑定的参考图、基线代码与已有预览、明确指定的候选输入和本任务已登记产出, 加入显式关联的历史结果及本任务结果。其他任务的记录不会因为位于同一黑板就自动进入上下文。

任务信息包含目标版本、基线、保护项、修改范围、假设和结束条件。候选及历史结果同时标明原始目标和基线, 观察与假设保留在不同字段中。`GenerationContext` 返回实际的多模态消息, 以及候选 ID、结果 ID 和已读取文件的清单。

相对文件路径按 `asset_root` 解析, 默认使用当前工作目录。指定了代码或预览路径但文件不可用时会返回错误; `preview_path=None` 明确表示没有提供预览。首版支持 PNG, 不缩放或裁剪图像。

例如, 在上面的黑板示例基础上:

```python
from pathlib import Path

from shader_deep.api import run_generation
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

生成角色的选材与每轮注入继续使用上述入口. 初稿与探索的 Builder 分别在 `agents/outline/context.py`、`agents/exploration/context.py`; 整合角色按当前阶段构造受限材料. `compatibility/` 保留旧协议及按需整合逻辑. 主分析具有完整请求的应用估算预算, 保留同组必要修复材料; 通过确定性规则整理已消费索引和已被当前完整草稿替代的历史参数, 保留调用配对, 不截断判断正文, 不额外调用模型生成摘要. 生成角色的聊天历史管理沿用 Deep Agents; 独立视觉评审尚未接入.

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
