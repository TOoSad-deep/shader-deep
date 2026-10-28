# Shader Deep

开发入口: [模块导航与架构](docs/architecture.md#模块导航) · [开发与验证](docs/development.md) · [文档同步约定](AGENTS.md#文档同步) · [设计决策](docs/decisions/0001-agent-oriented-layout.md). 历史改造与验证按需查对应任务记录.

下游开发设计: [整体架构与模块设计](docs/generation-architecture.md)提供目录、接口与四个实施分支入口; [宏观分析](docs/generation-execution-design.md)说明流程选择和审查依据. 01 至 04 已接入, 最新的[两元素整图组合](docs/work-items/generation-04-scene-composition.md)复用单方案生成与两方案比较产物; 真实模型效果仍需单独验收.

输入本地 PNG 和文字要求, 通过单个 Deep Agent 生成、渲染、查看预览并修正 Shader, 将选定候选的实际 GLSL 写入标准输出。

当前已连接内存黑板、Context Builder、真实 WebGL2 渲染工具和有预算的生成循环。每次尝试保存代码、预览或错误, 完成时明确选择已渲染且已向模型展示预览的候选。

独立分析提供 `shader-deep-analyze` 命令: 主 Deep Agent 登记元素、冻结目标和方向, 使用 SDK 原生 `task` 委派两个或三个独立探索子 agent, 再委派整合子 agent. 框架负责工具循环、skills、scratch 文件与上下文整理; 应用只保留固定输入、任务准入、五库校验和文件包交付. 整合仅合并 F/R/M, 草图由程序保义更新引用. 分析到生成仍需用户显式选择报告与草图, 候选仍需编码、渲染与视觉验收. 当前实现与迁移边界见[原生实施记录](docs/work-items/deep-agents-native-plan-20261028.md).

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

OpenRouter 分析请求在发送前移除字符串字段的非空白查找正则 `pattern: "\\S"`, 避免当前严格工具服务将完整文本约束成单字符. 字符串长度和其他正则保留, 本地仍拒绝空白文本并校验字段及引用; 不改变报告格式或放宽提交规则. 诊断及复测记录见[分析诊断](docs/work-items/analysis-diagnostic-20261025.md).

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

### 本地可视化界面

在本目录运行:

```sh
make web
```

然后打开 <http://127.0.0.1:8765>. 已有 `.env` 会由启动命令加载, 无需在网页填写密钥. 首次使用仍需先执行 `make sync`, 生成预览还需 `make browsers`.

界面支持上传 PNG、直接生成、多视角分析, 以及从本次会话的分析报告中显式选择草图/局部备选再生成. 页面显示日志、候选预览、真实完成状态和下载链接. 点击开始才会调用模型; 分析的调用预算沿用 YAML, 默认不限制总调用次数, 页面可停止运行. 生成默认 512 × 512、时间 0 秒、最多渲染 3 次; 报告模式也使用页面显式设置的尺寸.

同一服务同时只运行一个任务. 文件保存在 `runs/web/web-*/`, 原 CLI 产物位于各任务的 `output/run-*/`. 关闭或刷新浏览器不会取消任务; 终端 Ctrl+C 关闭服务并停止未完成任务. 页面历史只保留在本次服务会话, 重启后不自动恢复旧任务, 已落盘文件保留. 停止会终止本地任务, 不保证撤回模型提供方已接收的请求.

仅监听 `127.0.0.1`, 不提供远程部署或账号系统. 页面不读取任意本地报告路径, 不自动选择分析方案, 不接入多方案比较和整图组合. 本地服务可用不代表模型连接或视觉效果已通过验收. 实现与验证见[本地界面记录](docs/work-items/local-web-ui.md).

需要改端口或输出目录时:

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.web --port 8766 --output-dir runs/web
```

### 命令行

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

从完整五库报告执行一个明确方案:

```sh
uv run --no-sync --env-file .env shader-deep "只实现所选元素, 保持原画布位置" \
  --report /absolute/path/report --sketch S1 --background "透明" \
  --alternative 0 --max-attempts 3 --output-dir runs > output.glsl
```

`--alternative` 是可选的零基索引, 省略时采用所选草图的默认方案. 报告模式要求本次提示词、草图和背景, 不再传 PNG 位置参数. 未指定的宽高采用捕获原图对应尺寸, 只覆盖宽度时高度保持原图值; 旧 PNG 模式仍默认 512 x 512. 报告、原图和可选基线在启动模型前固定, 与候选共用一个运行目录.

Python 调用入口为 `run_generation_from_report`; 基线来自调用方已有黑板, CLI 首版只执行无基线的新报告任务:

```python
from pathlib import Path
from shader_deep.api import run_generation_from_report

outcome = run_generation_from_report(
    Path("/absolute/path/report"), "S1", "只实现所选元素",
    background="透明", alternative=0, max_attempts=3, output_dir=Path("runs"),
)
print(outcome.stop_reason, outcome.run_dir)
```

查看帮助无需模型配置:

```sh
uv run --no-sync shader-deep --help
```

PNG 字节保持原样, 任务说明与实际加载的图像一起进入多模态任务消息。模型连接沿用原来的 Chat Completions 配置。标准错误输出运行目录和最终预览位置; 标准输出只包含选定候选文件中的实际代码。

退出码: 完成并选定已渲染候选时为 `0`; 主动受阻或预算耗尽未完成时为 `1`, 不输出未经确认的 GLSL; 输入、配置或文件错误为 `2`. 执行异常向调用方抛出, 已有制品、错误类型与消息保留在运行目录, 异常注释标明目录.

## 单元素多视角分析

```sh
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "只分析中央粉色圆形本体" --output-dir runs > analysis-index.json
```

默认一批三个独立视角, `--max-tasks 2` 可以减少到两个. 第四个视角或新探索批次会被拒绝, 同一槽位只启动一次. 主 Agent 只定位元素与确定范围, 不预填特征观察或机制答案. 每个探索图收到固定原图、用户要求、目标范围与自己的方向, 使用独立模型历史和 scratch 文件. 父图 description 不能改写这些输入, 子图不能继续委派.


整合使用独立 Deep Agent 图, 仅在全部探索槽位成功或失败后启动, 读取固定 V0 并只合并已有特征、关系和机制. 程序拒绝改变方案含义的映射, 草图直接汇集且仅更新引用. 合法 V0 在整合启动前保存; 整合失败后交付 V0、`partial` 和具体缺口. 主图正常结束后程序核查实际产物, 无合法基线时返回 `failed`, 不发布空包. 模型结束文本或 scratch 文件不能代替合法业务提交.

### 文件包与读取

CLI 标准输出只返回 JSON 状态、运行目录、`report_dir` 和短结果索引, 标准错误实时显示关键步骤和入口位置. `AnalysisOutcome.report_dir` 是新增的可选返回属性, 公开函数签名保持不变; 结果协议及交付载体已从四库切换为五库.

```text
run-*/
  reference.png
  manifest.json     # 固定计划、槽位状态和已接受版本索引
  result.json       # 最终状态、选定版本及缺口
  results/          # 已接受的探索正文及 V0/V1
  run.json          # 公开入口的黑板与交付索引
  analysis.log      # 本轮业务进度与失败诊断
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

短入口为每套草图展示默认机制的 ID 与名称, 方便区分同名方案; 同一机制去重展示, 过多机制和长名称截断后可回读完整库. 导航顺序不表示方案排名或执行顺序.

按草图返回的是有范围的取材视图, 保留整轮状态、全部缺口和未知项. 视图提供有效选择及必要正文; 显式选中备选后, 也加载其理由与适用条件中引用的对象及正文依赖. 未选备选的材料继续从完整包按需读取, 不声称视图是自包含小包. 完整文件包可整体移动, 原图哈希和全部引用可校验. 已发布包不可覆盖.

### 实时运行日志

默认 `--log-level INFO` 将运行开始、业务进度、失败和最终状态输出到 stderr. 已接受正文保存在 `results/` 和 `report/`; SDK 模型与工具调用可沿用 LangSmith 追踪. 原生流程没有 attempt 代次或完整模型会话恢复日志.

```sh
# JSON 索引保存到文件, 关键进度仍显示在终端.
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析中央圆形" > analysis-index.json
# 终端也查看完整业务提交; 文件日志始终包含 DEBUG 记录.
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析中央圆形" --log-level DEBUG > analysis-index.json
```

`--log-level WARNING` 或 `ERROR` 可减少终端信息, 不减少文件日志. 后台执行时可将 stderr 重定向到控制台日志并使用 `tail -f` 查看; 也可直接查看运行目录的 `analysis.log`. Python API 始终保存文件日志, 控制台输出沿用宿主对 `shader_deep.analysis` logger 的配置.

日志属于诊断材料, 已接受报告由业务提交工具校验并保存. 执行成功不等于内容质量或视觉验收通过. 文本日志不记录图片 Base64、推理字段和客户端密钥. 已保存的旧运行与日志保留供人工检查, 当前流程不加载旧执行恢复协议.

### 配置与迁移

配置文件为 [resources/analysis.yaml](src/shader_deep/resources/analysis.yaml), 按模块位置加载. 显式 CLI 参数优先于 YAML, YAML 优先于 `AnalysisOptions` 内置值; 相对 YAML 输出路径基于配置文件所在目录. `--config` 可选其他文件, 显式文件不存在或字段错误会在模型调用前报错. Python 直接调用使用 `AnalysisOptions`, 可显式调用 `load_analysis_options` 加载 YAML.

`max_orchestrator_calls`、`max_integrator_calls` 和 `max_worker_calls` 分别限制主图、整合图和每个探索图的模型节点调用, 默认 0 不附加此限制; SDK 递归限制仍然生效. 这些值不限制所有子图的合计请求或框架摘要请求, 也不是费用预算. 阶段输出覆盖顺序保持: 阶段 CLI > 通用 CLI > 阶段 YAML > 通用 YAML > 内置值. `outline_max_output_tokens` 用于主角色, 其余两个阶段字段分别用于探索与整合.

网络暂时错误由框架模型重试中间件处理, 默认额外两次, SDK 客户端内部重试关闭. 工具提交错误可在同一图会话内修正; 会话结束仍无合法提交则记失败. 不自动重派、不启动恢复 agent、不在主图结束后补问模型. 每次公开调用创建新运行, 已落盘产物保留, 首版不续跑跨进程模型会话.

| 配置 | 原生流程处理 |
| --- | --- |
| `max_tasks`、`max_parallel` | 固定 2/3 个探索槽位, SDK task 并发上限 |
| 输出 token、超时、流式、网络重试 | 保留阶段优先级与传输适配 |
| `worker_observation_tools`、`max_measurements` | 固定原图的业务取证能力和每角色取证限制 |
| `max_main_calls` | 已删除; 使用独立的主图/整合图限制 |
| `max_repeated_no_progress`、`max_context_tokens`、`request_image_tokens`、`request_token_margin` | 已删除; 上下文管理采用 SDK 机制 |
| `max_comparison_targets`、`library_page_chars`、`max_integration_calls`、`max_integration_packages`、`integration_no_progress`、`integration_history_tokens` | 已删除; 当前角色不使用此协议 |

公开 `run_analysis` / `run_analysis_task` 的核心签名、CLI 状态索引和报告包格式保持; 以上执行与配置语义发生了明确变化. 表中的旧字段已从 `AnalysisOptions`、YAML 和 CLI 入口移除, 即使传入原默认值也会明确报错, 需要从调用参数或配置文件删除. 旧四库回放命令也不再提供.

草图最低产出要求继续另议. 当前按已表达的业务缺口计算部分完成, 不增加每个 worker 的草图数量门槛. `completed` 只说明本轮流程与已知必要工作完成, 不证明视觉效果、机制正确或上下文成本收益.

### 历史执行退出

旧四库、固定五库执行器、冻结回放和探索 skill 对照实验已移除. 当前保留原生分析、生成和五库报告读取入口, 已保存的运行目录及报告文件不删除. 四库报告不会自动转换或被五库读取器接受. 删除范围与验证见[清理记录](docs/work-items/analysis-legacy-cleanup-20261028.md).

当前实现见[架构说明](docs/architecture.md); 数据契约见[五库设计](docs/analysis-five-libraries.md); 原生角色分工见[分析架构设计](docs/analysis-architecture-design.md).

## 单 Agent 生成闭环

本轮只开放并允许执行三个工具:

| 工具 | 行为 |
| --- | --- |
| `render_shader(glsl_code)` | 保存本次代码, 使用固定 width、height、time 进行真实 WebGL2 编译和渲染, 登记候选与成功或失败结果 |
| `finish_shader(candidate_id, assessment)` | 选择本轮成功渲染且预览已进入后续模型上下文的候选, 保存自检说明并结束 |
| `stop_generation(reason, next_action, candidate_ids=None)` | 保存受阻原因和关联候选; 建议补充输入或另建任务改试方案, 不自动派发或选择候选 |

编译错误以工具反馈和结果记录返回。渲染成功后, 工具返回候选 ID, 下一次 Context Builder 会实际加载该 PNG, 与参考、基线、代码和相关结果一起交给模型。图片通过多模态任务消息提供, 不仅返回文件路径。

结束工具不能选择未渲染的代码, 也不能在首次渲染的同一批工具调用中跳过预览检查。完成后直接读取已选候选文件作为最终代码, 无需模型再生成一份可能不同的代码。模型自检和选择不代表用户已接受视觉效果。

默认最多渲染 3 个候选, 编译失败也计数. 普通逻辑模型请求上限为 `max_attempts + 2`, 自动模型摘要已关闭; 当前 SDK 每轮最多再做 2 次网络重试, 不计入 `model_calls`, 该字段不代表 HTTP 请求数或费用预算. 无效工具调用和纯文本回复仍受逻辑轮数限制.

`stop_generation` 的 `next_action` 只能为 `provide_input` 或 `try_another_scheme`. 受阻原因与建议保存在本任务 `blocked` 结果中; 同一响应里的后续工具也会遵守终态, 不再渲染或改写选择.

浏览器在一个专用执行线程中创建、使用和关闭。多个工具请求经过该线程串行处理, 浏览器可跨尝试复用, 尺寸、时间和尝试次数由代码控制。

每次在 `--output-dir` 下创建独立的 `run-*` 子目录:

```text
run-*/
├── candidate-001.glsl      # 第一次尝试, 失败代码也保留
├── candidate-002.glsl
├── candidate-002.png       # 仅成功渲染的尝试有 PNG
└── run.json                # 固定条件、计数、选择、停止原因和黑板快照
```

`run.json` 的 `stop_reason` 记录 `completed`、`blocked`、`attempt_limit`、`model_limit` 或 `error`. 未选中候选时 `selected_candidate_id` 为空; 若选择后清理资源失败, 保留已选候选供检查, 运行状态仍记为 `error` 且调用抛错. `error` 字段保留运行异常诊断. 报告模式额外保存 `inputs/report/` 和可选 `inputs/baseline/`, 基线副本映射位于 `run.json.inputs.baseline`. 快照更新采用临时文件替换, 不保存模型连接配置或 API 密钥.

## 两方案比较与人工选择

Python 入口 `run_generation_comparison` 接收同一报告中的两套显式选择, 顺序复用单方案生成闭环. 两项在任何模型请求前准备完毕, 只捕获一次原报告、原图与可选基线; 第二项从固定副本和内存材料准备, 不采用第一项的产出. 两项共用一次构造的模型配置, 各自创建 Agent、历史、额度和运行目录.

```python
from pathlib import Path
from shader_deep.api import GenerationPlan, run_generation_comparison

comparison = run_generation_comparison(
    Path("/absolute/path/report"),
    (
        GenerationPlan(name="默认遮罩", sketch_id="S1"),
        GenerationPlan(name="高斯备选", sketch_id="S1", alternative=0),
    ),
    "只实现圆形, 保持原画布位置",
    background="透明", max_attempts=3, output_dir=Path("runs"),
)
print(comparison.directory)  # 打开其中的 README.md 查看两项真实产物
```

比较目录包含 `comparison.json`、预览与代码导航 `README.md`, 以及 `runs/` 下两个独立子运行. 索引保存共同条件、方案来源和实际结果, 不复制子运行的代码或日志. 单项普通运行失败会保留错误并继续另一项; 用户取消或键盘中断停止整批, 已有子运行保留, 不发布完整比较索引.

人工选择初始为空. 查看预览后, 用 `select_generation_comparison(directory, item_index, candidate_id, reason=...)` 保存明确选择; `item_index` 为零基索引, 候选必须属于该项实际完成的选择. 更新前会核对子运行, 并保留其内容原样. `read_generation_comparison(directory)` 只读取已有索引. 即使异常运行保留了已渲染产物, 也不会被作为已完成条目接受选择. 这些接口不自动评分或启动整图生成, 视觉判断仍由用户给出.

## 两元素整图组合

`run_scene_generation` 接收两个明确选定的单元素来源运行及候选 ID, 原图从两个来源报告校验获得. 来源必须完成且候选确为该运行实际选择, 报告内容身份与原图哈希必须匹配保存的绑定. 不同报告可以使用相同短 ID, 但同一报告元素的两套方案不能冒充两个元素.

```python
from pathlib import Path
from shader_deep.api import SceneElementSource, run_scene_generation

outcome = run_scene_generation(
    (
        SceneElementSource(run_dir="/absolute/path/element-a-run", candidate_id="element-a-selected-id"),
        SceneElementSource(run_dir="/absolute/path/element-b-run", candidate_id="element-b-selected-id"),
    ),
    "组合两个元素, 保留各自关键外观",
    background="深灰", output_dir=Path("runs"),
)
print(outcome.stop_reason, outcome.run_dir)
```

两个来源可以是独立生成结果或比较中的已选子运行. 从比较索引读取来源时, 使用用户明确选择的条目及实际候选, 不默认采用第一项. `layout` 默认“保持原图布局与遮挡”, 可传入明确调整说明; 未指定宽高取自固定原图. 无需先标注每个元素的坐标和层级.

程序固定源报告、实际 GLSL 和可用预览, 每轮将完整原图、两份方案及代码交给现有生成 Agent. Agent 负责协调函数、坐标和叠加关系, 生成新的完整 `mainImage`; 程序不拼接 shader 字符串. 来源代码仅作为素材, 最终交付必须是整图会话自己渲染、回读并选中的候选.

`run.json.scene_plan` 保存组合要求与来源, `run.json.inputs.scene_elements` 保存报告绑定和本轮文件映射; 素材副本位于 `inputs/element-1/`、`inputs/element-2/`. 原来源运行保持原样, 不将两份黑板合并到新任务. 调整组合目标或来源时重新调用整图入口创建新任务, 本接口不从裸黑板恢复既有整图会话. 单 Pass、预算、主动受阻和异常语义沿用现有生成闭环; 模型自检不代替用户视觉接受.

## 当前目录与源码入口

```text
src/shader_deep/
├── api.py                  # 稳定 Python API
├── cli/                    # 分析、生成与本地网页命令
├── workflows/              # 调度、阶段推进、主进度与最终交付
├── agents/                 # analysis / generation 的角色定义与工具
├── domain/                 # 业务记录、五库契约与只读历史类型
├── infrastructure/         # 模型传输、配置读取、制品存储与追踪
├── imaging/                # 图像读取、测量与剖面
├── rendering/              # 独立 WebGL2 渲染器
└── resources/              # 默认配置与原生角色 skills
```

从[当前架构](docs/architecture.md)阅读职责、调用链和状态归属; 修改与验证方法见[开发指南](docs/development.md).

`workflows/analysis/` 固定本轮业务输入、槽位状态与交付; `agents/analysis/native.py` 定义原生主/子角色、工具与输入适配; `domain/five_libraries/` 维护字段与引用; `infrastructure/storage/report_package.py` 发布和读取文件包. SDK 管理模型历史、task 执行和 scratch.

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

未绑定报告的任务沿用上述 Context Builder 入口. 报告生成由公开 `run_generation_from_report` 统一准备, 内部每轮复用固定原图、有效方案和基线材料, 并刷新候选与错误; 不把带报告绑定的任务直接交给旧 `run_generation`. 当前分析在 `agents/analysis/` 按角色构造受限材料. 原生分析接受 SDK 的上下文摘要与工具输出外置; 生成不额外调用摘要模型, 保留有限轮数下的完整有效历史. 独立视觉评审尚未接入.

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

### 原图取证

`AnalysisOptions(worker_observation_tools=True)` 开放各探索角色绑定原图的裁剪与区域统计. 每个角色独立计量, 新证据须在后续请求实际回读后才能关联观察. 证据文件随五库包校验和固定, 规则见[辅助材料契约](docs/analysis-five-libraries.md#2026-10-27-内容收缩与辅助材料契约). 历史对照试验结果见任务记录, 当前包不提供实验执行入口.
