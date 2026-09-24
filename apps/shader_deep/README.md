# Shader Deep

维护入口: [当前架构](docs/architecture.md) · [设计决策](docs/decisions/0001-agent-oriented-layout.md) · [重构进度与验证](docs/work-items/structure-refactor.md) · [开发检查](docs/development.md).

输入本地 PNG 和文字要求, 通过单个 Deep Agent 生成、渲染、查看预览并修正 Shader, 将选定候选的实际 GLSL 写入标准输出。

当前已连接内存黑板、Context Builder、真实 WebGL2 渲染工具和有预算的生成循环。每次尝试保存代码、预览或错误, 完成时明确选择已渲染且已向模型展示预览的候选。

独立分析提供 `shader-deep-analyze` 命令：主 Agent 建立统一视觉初稿，子 Agent 独立探索三个候选库，独立整合 subagent 处理比较与复核, 后端校验并发布四库。输出协议为 `possibility_library_v1`。分析到生成的自动交接尚未接入，候选仍需编码、渲染与用户视觉验收。

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

### YAML 运行配置

分析模块专用配置位于 [resources/analysis.yaml](src/shader_deep/resources/analysis.yaml), 默认按模块位置加载, 不依赖启动目录, 并随应用打包。可直接修改其中的输出 token、主/子 Agent 调用次数、任务数、并发、取证、重试、超时和流式开关; 模型地址与密钥仍由 `.env` 提供。

优先级为 **命令行显式参数 > YAML > `AnalysisOptions` 内置默认值**。也可以选择其他配置文件, 并临时覆盖个别参数:

```sh
uv run --no-sync --env-file .env shader-deep-analyze test_pic/2d-physics-balls.png "分析构图、元素组织和可能机制" --config src/shader_deep/resources/analysis.yaml --max-worker-calls 5
```

YAML 使用平铺字段名, 例如 `max_output_tokens: 16384`、`max_worker_calls: 3`、`stream_model_responses: true`。当前模块 YAML 将主、子 Agent 的累计调用上限均设为 0, 主任务仍受每阶段及比较组预算限制; 通用输出上限设为 163840、请求超时设为 240 秒. `outline_max_output_tokens`、`integration_max_output_tokens`、`worker_max_output_tokens` 可分别覆盖初稿、整合和子任务输出上限, 默认均为 `null`, 回退通用值.

阶段输出的优先级为 **阶段专用 CLI > 通用 `--max-output-tokens` CLI > 阶段 YAML > 通用 YAML > 内置默认值**. 对应阶段 CLI 为 `--outline-max-output-tokens`、`--integration-max-output-tokens`、`--worker-max-output-tokens`. 同一有效值用于模型请求参数、上下文输出预留和阶段预算记录; 实际可用上限仍取决于模型服务. 文档中的回放示例值不会自动成为默认配置.

未传 `--config` 时读取模块内的 `config.yaml`, 不自动读取当前目录的同名文件; 模块内文件缺失时使用内置默认值。显式指定的文件不存在、字段拼错、YAML 类型或预算范围错误时, 在调用模型前以退出码 2 报错。YAML 中的相对 `output_dir` 基于配置文件所在目录, CLI 的 `--output-dir` 仍基于当前工作目录; 默认配置的 `output_dir: null` 使用当前工作目录下的 `runs`, 避免把运行产物写入源码目录。最终有效参数继续写入 `run.json` 的 `options`。

Python 调用方可显式使用同一加载器, 现有 `run_analysis` 接口保持不变:

```python
from pathlib import Path
from shader_deep.api import run_analysis
from shader_deep.analysis.config import load_analysis_options

options = load_analysis_options()  # 默认读取分析模块内的 config.yaml.
outcome = run_analysis(Path("test_pic/2d-physics-balls.png"), "分析参考图", options=options)
```

配置读取使用已由 SDK 锁定并安装的 PyYAML `safe_load`, 本应用将其声明为直接依赖, 不引入新的解析器或升级现有版本。

主、子 Agent 的 `max_main_calls` 与 `max_worker_calls` 默认均为 0, 表示没有额外的对应累计调用上限; YAML 与 Python 默认值一致, CLI 对应参数可显式覆盖. 主任务仍受下面的每阶段、每比较组、无进展和完整请求预算约束. 网络重试最多额外 2 次, 独立测量总额为 8; 正整数调用上限可进一步限制整体调用数.

### 分析执行与预算

主 Agent 负责初稿规划, 独立整合 subagent 负责后续受控整合. 主 Agent 首次接收用户要求和完整原图, 一次 `submit_visual_outline(outline, directions)` 提交统一初稿和至少两个中性探索问题. 后端冻结本轮元素与实例组身份, 按方向运行独立探索批次; 所有探索子任务终止后, 协调器同步委派一次 `IntegrationSubagent`. 探索子任务只接收原始要求、统一初稿、探索方向和完整图像, 不传递兄弟报告或测量.

子任务以三个顶层参数提交 `sketch_library`、`feature_library`、`relation_library`, 不再套 `report`. 后端校验后直接入统一库, 在短写锁中串行发布; 同一成功提交重放不重复入库, 不同内容不能覆盖原报告.

默认完整入口与 `analysis.replay` 共用 `ManagedIntegrationSession` 协调器和 `IntegrationSubagent` 整合角色. 整合子 Agent 先处理初稿问题, 再发现拥有者比较组, 顺序执行特征、关系、草图比较; 最后根据实际归并后的规范身份发现候选比较组. 发现阶段用 `plan_comparisons` 一次提出有依据的同类型小组, 每组 2 至 6 个本轮真实 ID; 程序绑定工作身份、材料和版本. 未选条目原样保留, 不宣称整库已去重. 完整发现材料放不下时记录缺口, 不截断后冒充全量发现.

整合通过 `IntegrationInput` 接收业务快照、初稿版本、探索反馈、原图和受控库服务, 不继承主 Agent 的聊天历史或修复草稿. 子角色拥有独立执行计数和 `A1-integration` 等追踪身份, 返回 `IntegrationOutcome` 中的比较结果、修订和缺口; 协调器负责最终交付. 库服务在探索终止后交给整合串行使用, 修改继续经过原有引用、呈现、版本和幂等校验.

新增 `integration-subagent.json` 保存子角色状态, `integration-work.json` 保留各工作回执. 子角色每次检查点落盘后通过进度回调通知协调器刷新主 `run.json`, 同步调用计数、比较进度、初稿修订和缺口; 累计进度按快照替换, 不重复追加缺口. `run.json` 的 `coordinator_execution` 和 `integration_execution` 分别记录两种角色, `main_execution` 为兼容既有统计保留初稿加整合的累计口径. 整合事件和工具记录标明子角色及父任务; 失败或中断时先保存子角色结果, 再由协调器交付已发布的部分库.

比较步骤只开放 `submit_integration` 与局部修复, 字段限于当前类型的 `merges`、`preserve`、整组 `deferred_work`. 程序提供目标正文及候选的直接必要前提, 请求通过预算检查并实际发送后才登记呈现. 普通比较使用纯文本, 不开放测量、扩大范围、元素未决修改或任意引用更新. 特征按范围和外观判断, 候选按机制及必要前提判断; 合并拥有者保留全部候选. 后端继续校验引用、来源和整笔事务, 无法合法发布的工作明确暂缓.

初稿问题由 `review_outline` 逐项提出驳回、暂缓或受限修订. 修订只允许已有元素的 `salient_features` 和已有关系的描述文字, 不改身份、范围、实例或端点, 原探索输入与报告保留. 整个初稿和完整库材料实际呈现后才接受修订, 保存新初稿版本, 然后由下一次独立请求调用 `verify_outline` 复核. 提案本身不能关闭问题; 复核未确认、缺预算或需要新增/拆分对象的情况仍保留 `partial`.

工作预算耗尽或无法判断时暂缓本项并继续其他独立项, 不自动重新领取暂缓项. 总调用或阶段数额度耗尽时停止安排新项, 保留已发布结果. 每次请求提供工作和全局剩余调用额度; `max_main_calls=0` 仅取消全局总次数限制, 不取消每阶段限制. 全部安排结束后由程序检查必要工作、失败任务、初稿问题和覆盖缺口, 自动形成真实终态. `integration-work.json` 保存各阶段和工作的实际调用、回执及失败原因. 原按需读取和测量实现仍保留底层兼容回归, 不是默认模型工具列表.

初稿与整合分别维护格式修复状态, 按初稿、发现阶段或稳定 `work_id` 分别计数, 每个作用域最多两次修复调用; 同组刷新、重建循环或暂缓后接续不重置额度, 全局仍累计统计. 已消费目录回执在请求副本中替换为有界计数和游标, 保留工具调用配对. 当前完整草稿与错误由控制上下文提供, 被替代的旧提交参数不重复驻留; 草稿仍无法完整容纳时保存 `repair_context_exceeded` 并暂缓工作, 不截断草稿或发布部分操作.

原有 YAML / CLI 配额继续有效. `max_main_calls` 约束初稿与整合调用之和, 派发时扣除初稿已经使用的额度; 剩余为零不会被解释为无限制. 探索调用仍由 `max_worker_calls` 独立限制. 新增配置可通过 YAML 或 `AnalysisOptions` 设置:

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `max_context_tokens` | 262144 | 主任务完整请求的应用估算预算, 包括输出预留 |
| `request_image_tokens` | 4096 | 每图估算预留 |
| `request_token_margin` | 2048 | 请求编码和估算余量 |
| `max_comparison_targets` | 12 | 单组显式目标上限; 超限必要工作保持未完成, 不静默拆分 |
| `library_page_chars` | 8000 | 库目录、工作和问题索引的单页 JSON 字符上限 |
| `max_integration_calls` | 12 | 每个比较或发现阶段及初稿阶段的最大调用数, 包含修复 |
| `max_integration_packages` | 24 | 整合阶段次数, 包括发现、初稿复核和每项比较 |
| `integration_no_progress` | 3 | 未配置正数全局无进展阈值时的阶段停止阈值 |

主请求发送前检查系统提示、工具 Schema、材料、测量图像、修复历史和输出预留. 文本按每字符一个估算 token 加图像与额外余量计算; 这是应用的保守估算策略, 不是提供方 tokenizer 的精确计数或上下文窗口声明. 真实用量仍从提供方响应读取. 探索子任务保留自己的调用预算, 不套用整合预算.

当前结果外层为 `analysis_protocol="possibility_library_v1"`, `analysis_detail` 为 `elements` 加三个库. 旧 `LensReport` / `AnalysisSummary` 可识别和校验, 不自动转换; 公开入口拒绝未经转换的历史报告输入. 公开函数签名及 CLI 调用方式不变.

失败任务、未解决初稿问题、未覆盖元素、预算出口和 `deferred_work` 均保留, 交付可为 `partial`. 没有合法子报告时保留失败状态. `completed` 不证明跨包语义比较全部完成、候选机制真实或视觉效果被验收. 运行目录保留原图、原报告、版本、来源、提交回执、材料队列与缺口; 库快照是批量发布的权威记录, 不宣称可恢复完整模型会话. 新版真实两图测试和用量对照仍需单独验证, 不依据无网络测试声称视觉或 token 收益.

`tools.jsonl` 保存主任务工具调用参数及成功或拒绝回执; `events.jsonl` 保存阶段有效输出额度、请求大小和提供方实际用量等记录. 冻结探索输入可单独回放主整合, 无需再次运行探索:

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.replay /absolute/path/source-run \
  --output-dir runs/replay --max-main-calls 8 --integration-max-output-tokens 32768
```

源目录须包含受支持的 `run.json` 和原始 `reference.png`; 回放校验原图哈希并恢复冻结初稿、原探索报告、问题和失败状态, 创建独立目录, 不继承旧整合决定或计数. 它会调用配置的模型服务, 只验证固定输入下的整合行为, 不代表完整单图实测或已经取得成本收益.

固定样本的受控去重实验使用独立入口, 依次执行卡片投影特征比较、依赖其拥有者合并的四个候选比较、独立的底场晕影特征比较:

```sh
uv run --no-sync --env-file .env python -m shader_deep.experiments.controlled_replay \
  runs/real-business-budget-balls-20260922-54CxRg/run-avu0ozrm \
  --output-dir runs/controlled-dedup --max-main-calls 12 --max-work-calls 4 \
  --integration-max-output-tokens 65536
```

这是绑定该来源对象身份的实验路径. 每项仅提供完整文本材料及合并、保留、暂缓工具, 不重新探索或测量; 依赖不满足时跳过候选比较, 剩余额度允许时继续独立项. 特征按对象范围和外观等价判断, 合并保留全部候选; 候选按机制与必要前提等价另行判断, 不因外观相似而吞并不同机制. `controlled-summary.json` 分别记录三项结果, 暂缓项不自动重入. 原失败任务与初稿问题继续保留, 因而局部三步通过时全运行仍可为 `partial`; 语义判断须对照原文检查, 不以数量下降证明正确或宣称完成整库去重.

详细契约与工具见 [独立探索与可能性库](docs/analysis-protocol.md). [旧协议记录](src/shader_deep/analysis/README.md)保留历史, 不代表当前工具接口.

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
├── agents/                 # outline / exploration / integration / generation
├── domain/                 # 业务记录、引用、四库与发布规则
├── runtime/                # 调用循环、预算、历史、提交与修复
├── infrastructure/         # 模型传输、配置读取、制品存储与追踪
├── imaging/                # 图像读取、测量与剖面
├── rendering/              # 独立 WebGL2 渲染器
├── resources/              # 默认 YAML
├── compatibility/          # 显式保留的旧流程
└── experiments/            # 固定样本回放实验
```

从 [当前架构](docs/architecture.md) 阅读完整职责、调用链和状态归属; 修改与验证方法见 [开发指南](docs/development.md). `analysis/`、旧 `context/`、`tools/` 及旧模块路径是兼容转发, 新实现不从这些路径导入.

`workflows/coordinator.py` 组合初稿、探索和整合执行; `workflows/dispatch.py` 管理批次, `workflows/delivery.py` 检查整轮交付, `workflows/progress.py` 更新主快照. 整合角色不再继承历史探索会话.

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
