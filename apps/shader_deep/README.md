# Shader Deep

项目状态: [总体进度、当前重点与新思路](docs/progress.md). 跨会话继续工作时先从这里恢复全局, 再进入对应任务记录.

开发入口: [模块导航与架构](docs/architecture.md#模块导航) · [开发与验证](docs/development.md) · [文档同步约定](AGENTS.md#文档同步) · [设计决策](docs/decisions/0001-agent-oriented-layout.md). 历史改造与验证按需查对应任务记录.

下游开发设计: [整体架构与模块设计](docs/generation-architecture.md)提供目录、接口与四个实施分支入口; [宏观分析](docs/generation-execution-design.md)说明流程选择和审查依据. 01 至 04 已接入, 最新的[两元素整图组合](docs/work-items/generation-04-scene-composition.md)复用单方案生成与两方案比较产物; 真实模型效果仍需单独验收.

输入本地 PNG 和文字要求, 通过单个 Deep Agent 生成、渲染、查看预览并修正 Shader, 将选定候选的实际 GLSL 写入标准输出。

当前已连接内存黑板、Context Builder、真实 WebGL2 渲染工具和有预算的生成循环。每次尝试保存代码、预览或错误, 完成时明确选择已渲染且已向模型展示预览的候选。

独立分析提供 `shader-deep-analyze` 命令，一个原生 Deep Agents 角色观察整图，提交上下文、多元素 E、特征 F 与关系 R。应用交付 `scene_observations_v1` 后，`shader-deep-plan` 由一个规划角色组织整图 M/S，发布 `scene_planning_v1`。调用方显式选择 S 后，通过生成入口得到 Shader 与预览。观察实现见[单 Agent 记录](docs/work-items/single-agent-observations-20261110.md)，规划入口见[规划工作流](src/shader_deep/workflows/planning/README.md)。

## 独立视觉评审

`shader-deep-review` 接收参考 PNG、实际渲染 PNG 和固定任务 JSON, 发布 `visual_review_v1` 报告包. 一个评审角色按任务选择特征、元素或整图模板, 对照六维检查内容记录可见差异. API 为 `run_review(reference_path, rendered_path, request, *, options=None)` 与 `read_review_package(directory)`.

在本目录创建任务文件, 下例聚焦圆形轮廓与边缘这一特征.

```json
{
  "scope": "local",
  "target_kind": "feature",
  "target": "前方圆形的轮廓与边缘",
  "requirements": "本轮检查轮廓和边缘, 其他外观留待后续评审",
  "dimensions": [
    {"id": "content", "required_checks": ["目标圆形存在"]},
    {"id": "layout", "excluded_checks": ["本轮不检查位置和画面比例"]},
    {"id": "shape", "required_checks": ["轮廓、厚度和边缘软硬符合参考"]},
    {"id": "color", "excluded_checks": ["本轮不检查色场"]},
    {"id": "surface", "excluded_checks": ["本轮不检查表面细节"]},
    {"id": "relations", "excluded_checks": ["本轮不检查与其他元素的关系"]}
  ]
}
```

将其保存为 `review-task.json`, 使用现有模型配置执行.

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.review \
  reference.png rendered.png --task review-task.json --output-dir runs/review
```

安装或同步后的 console script 为 `shader-deep-review`. 预算默认 3 次逻辑模型调用, YAML 由 `--config` 选择, 显式 CLI 参数覆盖对应配置. `output_dir` 在 YAML 中按配置文件目录解析. 参考与渲染路径按当前目录解析.

`local` 必须指定 `target_kind=feature` 或 `element`, `scene` 省略该字段. 六个指标都需出现, 没有必查内容时填写排除理由. 可选 `reference_region` 和 `rendered_region` 使用 `{left, top, right, bottom}` 原始像素坐标, 对应左闭右开的区域. 系统保存完整图片与原像素裁剪, 根据固定标准评审. 呈现时间与背景未知时可以省略 `conditions`, 必要证据不足会进入未评项.

如需追溯生成运行，可在任务中填写 `source.run_id` 和 `source.candidate_id`。请先核对所选 PNG 确属该候选。报告会保留这两个标识，评审模型看不到它们。

标准输出为运行状态、视觉结论和报告目录的 JSON 索引. `completed` 表示报告交付, `verdict` 分别表达 `needs_revision`、`no_major_issue_observed` 或 `inconclusive`. 合法报告交付返回退出码 0, 受阻或运行失败返回 1, 启动前输入错误返回 2. 报告可用 `read_review_package` 核对固定材料、标准和结论.

Python 调用可从任务 JSON 构造请求.

```python
from pathlib import Path

from shader_deep.api import ReviewRequest, read_review_package, run_review

request = ReviewRequest.model_validate_json(Path("review-task.json").read_bytes())
outcome = run_review(Path("reference.png"), Path("rendered.png"), request)
if outcome.report_dir is not None:
    package = read_review_package(outcome.report_dir)
    print(package.report.verdict, package.report.submission.summary)
```

独立会话只读取评审材料并提交自己的报告. 局部结果的适用范围随报告保留. 模块说明见[评审角色](src/shader_deep/agents/review/README.md)和[评审工作流](src/shader_deep/workflows/review/README.md), 分级依据见[指标准则](docs/visual-review-rubric.md), 验证记录见[实施方案](docs/work-items/independent-visual-review-implementation-20261117.md).

任务 JSON 可选填 `samples` 或 `coverage`，分别启用区域测量和必查部位覆盖检查。报告回读后，程序证据位于 `package.report.evidence`。

- `samples` 中每项给出 `label`、`reference_region` 和 `rendered_region`，两个矩形使用各自输入 PNG 的像素坐标。若任务指定了局部视图，采样须位于相应视图内。以磨砂叠圆为例，可以分别指定前圆内部和空背景两个样本。程序测量每侧的 RGB 均值、加权亮度和像素数，使用 0–255 编码 RGB 的单位及白底透明合成。区域测量接受最高 8 位 PNG，16 位输入可继续做视觉和覆盖评审。
- `coverage` 中的 `reference_window` 和 `rendered_window` 描述整个输入 PNG 在共享坐标系中的 1:1 像素窗口。每个窗口的尺寸须与对应 PNG 一致。若任务另指定了局部视图，实际覆盖以该局部视图平移到共享坐标系后的矩形为准。每个 `required_regions` 条目提供 `dimension`、`check`（该维度必查项的原文）和共享坐标的 `region`。程序把任一侧未覆盖的必查项登记为未评。视图完整时，元素外观是否缺项仍以可见图像为依据。
- Python 调用方可从 `shader_deep.api` 导入 `ReviewSample`、`PixelRegion`、`ReviewCoverage` 和 `RequiredRegion`。调用方负责提供可信的坐标对应关系；窗口尺寸校验适用于 1:1 平移。

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

规划图名称为 `scene-planner`，metadata 记录 `planning_run_id` 和 `observation_sha256`。规划本地索引保存实际模型、显式推理配置、规则资源哈希、调用数和 SDK 报告的 token usage。观察、规划和生成沿用同一项目追踪配置。

非默认区域的账户需要设置对应的 `LANGSMITH_ENDPOINT`; 多工作区密钥可能还需 `LANGSMITH_WORKSPACE_ID`。设置 `LANGSMITH_TRACING=false` 可关闭原生环境追踪。未配置时生成流程仍可正常运行。配置方式见 [LangSmith 官方文档](https://docs.langchain.com/langsmith/trace-with-langchain)。

## 运行

### 本地可视化界面

在本目录运行:

```sh
make web
```

然后打开 <http://127.0.0.1:8765>. 已有 `.env` 会由启动命令加载, 无需在网页填写密钥. 首次使用仍需先执行 `make sync`, 生成预览还需 `make browsers`.

界面支持上传 PNG、直接生成、单 Agent 全图观察, 以及从旧完整五库报告中显式选草图/局部备选再生成. 页面显示日志、候选预览、真实状态和下载链接. 点击开始才调用模型; 分析默认最多 20 次主模型判断, 可沿用 YAML 修改预算, 页面可停止运行. 新观察结果展示 E/F/R, 不提供尚未规划的草图选择. 生成默认 512 × 512、时间 0 秒、最多渲染 3 次; 报告模式也使用页面显式设置的尺寸.

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

## 单 Agent 全图观察

```sh
uv run --no-sync --env-file .env shader-deep-analyze /absolute/path/reference.png "分析整张图的元素、外观和关键配合" --output-dir runs --max-model-calls 8 > analysis-index.json
```

一个角色读取整图与要求, 在 E 下记录 F, 在场景层记录 R. 首次保存完整草稿, 回读 review 后局部修订, 完成时冻结并发布. 同类重复实例可形成一个对象群, 元素数量按实际场景确定. 粒度见[观察规范](docs/analysis-observation-guidelines.md)与[五图样例](docs/analysis-observation-examples.md).

业务工具为 `save_observation_draft`、`finish_analysis` 和 `stop_analysis`. SDK `read_file` 回看规则、草稿与本轮外置材料, `edit_file` 仅编辑 `/draft/observations.json`. 首次保存只创建草稿, 后续采用唯一匹配的局部替换. 同批工具按模型声明顺序执行, 编辑按前一步实际提交版本继续, 终态后的操作拒绝. 模型可见工具和执行准入共同限制权限.

保存和编辑检查字段结构, 重复 ID 与悬空引用等业务问题可暂存并返回定位信息. Agent 根据反馈修订, 完成前回读完整最新稿. `finish_analysis()` 从文件执行完整校验并冻结结果. 运行流程与验收要求见[草稿实施记录](docs/work-items/observation-draft-edit-plan-20261111.md).

参考图在入口捕获一次, 请求级保持可见; 规则固定提供, 详细样例按需读取. 消息和摘要由 SDK 管理, 规则样例在代码版本中维护, 不自动积累跨运行经验. 接受结果后在模型链之前结束, 不再发起主模型或摘要请求.

### 文件包与读取

CLI stdout 只输出状态、运行目录、report_dir 和结果索引 JSON; stderr 输出诊断. `run_analysis` / `run_analysis_task` 的签名和 `AnalysisOutcome` 字段保持, 默认协议改为 `scene_observations_v1`.

```text
run-*/
  reference.png
  manifest.json           # 观察会话进度, 不含模型历史
  draft/
    observations.json     # 唯一可编辑草稿
    history/<sha256>.json # 编辑前快照
  results/observations.json # finish 接受时的冻结快照
  result.json             # 最终状态与错误
  run.json                # 黑板、实际协议与生效配置
  analysis.log
  report/
    manifest.json         # 状态、原图身份、上下文、未知项和缺口
    elements.json         # E 和程序生成的 feature_ids
    features.json         # F 的可见观察
    relations.json        # R 的参与项和配合
    reference.png
    README.md             # 程序生成的全图树状视图
```

```python
from pathlib import Path
from shader_deep.api import run_analysis, read_observation_package
from shader_deep.workflows.configuration import load_analysis_options

outcome = run_analysis(Path("/absolute/path/reference.png"), "分析全图", options=load_analysis_options())
if outcome.report_dir is not None:
    manifest, observations = read_observation_package(outcome.report_dir)
    print(manifest.context_notes, observations.elements, observations.relations)
```

短 ID 保留模型修订后的编号和顺序. 每个 F 只有一个主归属, R 可以跨 E/F. 可见背景登记为 E, context_notes 补充全图要求或条件. 文件包正式回读验证引用、归属和原图哈希后发布, 已发布目录受保护.

finish 接受的观察有必要缺口时为 `partial`, 尚未完成接受或发布失败时为 `failed`. 失败或预算耗尽时已有草稿保留在 draft/ 供排查. `completed` 仅表示观察合法交付且未声明必要缺口, 不证明无遗漏或视觉正确. 写包失败保留接受快照, 不登记成功结果. 中断尽量保存已有合法产物后继续传播; 文件记录不承诺恢复完整模型会话.

`read_report_package` / `read_sketch` 读取 `five_libraries_v1` 旧完整包。新观察包通过 `run_planning` 形成独立规划包，再由 `run_generation_from_planning` 消费显式选定的整图方案。

### 配置与迁移

配置来自 [analysis.yaml](src/shader_deep/resources/analysis.yaml), 路径按模块定位. CLI 显式值优先于 YAML, YAML 优先于内置值. YAML 中的相对 output_dir 以配置文件所在目录为准. Python 可显式加载 YAML 或传入 AnalysisOptions.

`max_model_calls` 默认 20, 必须为正整数, 限制普通主模型判断; SDK 摘要和网络重试单独计量, 不是所有 HTTP 请求总数. `max_output_tokens` 是唯一观察角色的通用输出预算. 网络重试、超时和流式传输沿用现有适配. 未提交即结束时不在图外补问.

| 旧字段 | 迁移行为 |
| --- | --- |
| max_tasks、max_parallel、max_measurements | 旧默认 3、3、8 仅作为兼容占位; 非默认值报迁移错误 |
| max_worker_calls、max_orchestrator_calls、max_integrator_calls | 旧默认 0 为占位, 不代表新流程无限调用; 非 0 改用 max_model_calls |
| worker_observation_tools | false 为占位; true 不受支持 |
| outline/worker/integration_max_output_tokens | null 为占位; 非 null 改用 max_output_tokens |

最终迁移语义在 CLI 覆盖合并后、模型构建前校验; 旧显式占位给诊断. Python 构造器不能区分显式默认和省略, 按值处理. `max_tasks=1` 不是单 Agent 开关. 默认只运行一个观察角色, 不保留旧多视角执行作为回退.

### 日志与验证边界

`--log-level INFO` 实时输出业务日志; WARNING/ERROR 可减少终端信息, 文件仍保留诊断. 已接受内容在 results/ 和 report/, 可沿用配置的 LangSmith 追踪. 普通日志不记录密钥或 PNG Base64, 不在常规离线测试中调用外部模型.

当前代码与离线检查见[实施记录](docs/work-items/single-agent-observations-20261110.md). 五图真实模型与人工视觉校准单独记录, 不能以模拟模型测试或文件校验代替. 跨模块流程见[架构说明](docs/architecture.md).

## 单 Agent 全图规划与生成交接

`run_planning` 接收已发布观察包和本次整图要求，捕获原图、完整 E/F/R、上下文、观察问题与 gaps，以及当前单 Pass 生成约束。一个规划 Agent 维护 M/S 草稿，全部 S 共享同一份完整观察范围。业务字段见[轻量五库设计稿](docs/work-items/five-library-planning-contract-20261111.md)。

### 命令行

在本目录通过模块入口运行规划。

```sh
OPENROUTER_REASONING_EFFORT=low uv run --no-sync --env-file .env python -m shader_deep.cli.planning \
  /absolute/path/observation-report "按原图规划完整场景的实现方案" \
  --output-dir runs/planning > planning-index.json
```

这是本项目当前 GLM 模型的命令示例。前缀通过现有模型配置机制设置本次进程的推理 effort，实际值记录在 `execution.reasoning_effort`。Python API 沿用调用进程的环境与模型设置。

stdout 输出 `status`、`run_dir`、`report_dir` 和 `error`。从正式包的 sketches.json 确定方案 ID，再显式启动整图生成。

```sh
uv run --no-sync --env-file .env python -m shader_deep.cli.generation \
  --planning-report /absolute/path/planning-report --sketch S1 \
  --max-attempts 3 --output-dir runs/generation > output.glsl
```

整图要求和背景安排来自固定规划包。选择局部备选时追加 `--alternative 0`，索引从零开始。省略该参数时采用所选 S 的默认方案。

规划默认采用原图尺寸和时间零，可通过 `--width`、`--height`、`--time` 固定预期生成条件。生成时省略的尺寸与时间继承规划快照，仅设置 `--max-attempts` 继续使用这些条件。显式渲染覆盖记录为本轮执行条件，差异与原规划假设共同进入生成请求。

### Python 入口

```python
from pathlib import Path
from shader_deep.api import (
    PlanningOptions,
    read_planning_package,
    read_planning_sketch,
    run_generation_from_planning,
    run_planning,
)

planned = run_planning(
    Path("/absolute/path/observation-report"),
    "按原图规划完整场景",
    options=PlanningOptions(output_dir=Path("runs/planning")),
)
if planned.report_dir is not None:
    package = read_planning_package(planned.report_dir)
    print(package.manifest.status, [item.id for item in package.report.sketches])
    selected = read_planning_sketch(planned.report_dir, "S1")
    print(selected.complete, selected.observation_gaps, selected.gaps)
    generated = run_generation_from_planning(planned.report_dir, sketch_id="S1")
    print(generated.stop_reason, generated.run_dir)
```

`run_generation_from_planning` 的 `options=None` 继承规划尺寸与时间。显式传入完整 `GenerationOptions` 时，渲染字段按该对象执行。调用方调整预算时，可基于规划条件构造新的完整选项。

### 工具、规则与交付

规划工具为 `save_planning_draft`、`read_file`、`edit_file`、`finish_planning` 和 `stop_planning`。首次保存后，Agent 通过 `/draft/planning.json` 局部修订，完整回读最新版本并实际进入下一次模型请求后完成接受。

主 [scene-planner/SKILL.md](src/shader_deep/resources/skills/scene-planner/SKILL.md) 提供共同流程。方法拆合、备选迁移与条件缺口的复杂判断分别通过条件参考读取。具体工具权限和请求装配见[规划角色](src/shader_deep/agents/planning/README.md)。

方法 conditions 随选中方案进入生成材料，组织说明交代实际输入来源。上游观察 gaps 与 S.gaps 各自保留，候选索引由程序派生。程序校验字段、引用和覆盖，方法可执行性通过生成与审阅核对。

至少一套 S 没有已知必要缺口时，合法发布状态为 `completed`，其余合法规划为 `partial`。具体生成始终采用所选 S 的完整性和两类缺口。发布失败或尚未接受时为 `failed`，已有草稿与接受快照保留在运行目录。规划合法交付、Shader 渲染与人工视觉验收分别记录。

运行目录包含 `inputs/` 固定材料、`draft/` 可编辑正文及历史、`results/planning.json` 接受快照、`report/` 自包含正式包、`manifest.json` 和 `result.json`。正式包使用 `scene_planning_v1`，绑定完整观察内容身份和规划文件摘要，支持迁移目录后读取。

配置见 [planning.yaml](src/shader_deep/resources/planning.yaml)，初始 `max_model_calls` 为 20。CLI 显式参数覆盖 YAML，YAML 相对输出目录基于配置文件所在目录。正常入口、局部编辑、分页回读、异步执行、两类 gaps、失败发布和 CLI 的离线行为覆盖见[规划测试](tests/unit_tests/workflows/test_planning.py)与[原生图测试](tests/unit_tests/agents/test_native_planning.py)。

## 单 Agent 生成闭环

默认生成模式开放并允许执行三个工具; 显式启用视觉记忆后增加两种回看工具:

| 工具 | 行为 |
| --- | --- |
| `render_shader(glsl_code)` | 保存本次代码, 使用固定 width、height、time 进行真实 WebGL2 编译和渲染, 登记候选与成功或失败结果 |
| `finish_shader(candidate_id, assessment)` | 选择本轮成功渲染且预览已进入后续模型上下文的候选, 保存自检说明并结束 |
| `stop_generation(reason, next_action, candidate_ids=None)` | 保存受阻原因和关联候选; 建议补充输入或另建任务改试方案, 不自动派发或选择候选 |
| `list_materials(kind=None, limit=20, cursor=None)` | 仅记忆模式: 列出本轮允许材料的简短目录 |
| `read_materials(items)` | 仅记忆模式: 按材料 ID 回读图像、裁剪或正文, 多份材料可同轮对照 |

编译错误以工具反馈和结果记录返回。渲染成功后, 工具返回候选 ID, 下一次 Context Builder 会实际加载该 PNG, 与参考、基线、代码和相关结果一起交给模型。图片通过多模态任务消息提供, 不仅返回文件路径。

结束工具不能选择未渲染的代码, 也不能在首次渲染的同一批工具调用中跳过预览检查。完成后直接读取已选候选文件作为最终代码, 无需模型再生成一份可能不同的代码。模型自检和选择不代表用户已接受视觉效果。

默认最多渲染 3 个候选, 编译失败也计数. `--max-model-calls` 可独立指定模型轮数; 省略时原模式使用 `max_attempts + 2`, 视觉记忆模式使用 12. 自动模型摘要已关闭; 当前 SDK 每轮最多再做 2 次网络重试, 不计入 `model_calls`, 该字段不代表 HTTP 请求数或费用预算. 无效工具调用和纯文本回复仍受逻辑轮数限制.

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

### 按需视觉记忆

通过 `--visual-memory` 显式开启; 默认关闭, 保留原流程作为兼容入口和对照:

```sh
uv run --no-sync --env-file .env shader-deep /absolute/path/reference.png "根据参考图生成 Shader" \
  --visual-memory --max-model-calls 12 --max-attempts 3 --output-dir runs > output.glsl
```

同样可用于旧完整五库 `--report` 单方案模式. 首版不支持 `--planning-report` 或整图组合输入开启记忆; 比较/组合保持各自原有行为.

记忆模式持续提供参考图、固定要求及已选方案, 默认展开最新成功候选; 旧材料通过目录和 ID 回读. 回看图像或正文只在紧接着的一轮展开, 原文件继续保存. 同一候选的代码、预览和结果保留关联, 完成仍要求本轮成功渲染且完整预览已经呈现; 局部裁剪不能代替整图资格.

本轮新增 `memory/index.json`、`memory/derived/` 和 `candidate-*-render.json`, 分别保存材料索引、派生裁剪和单次渲染结果. 错误与代码可按 ID 回读, 运行目录不会恢复模型会话. 详细选材与状态归属见[生成架构](docs/generation-architecture.md#按需视觉记忆), 验证证据见[实施记录](docs/work-items/vista-minimal-harness-implementation-20261111.md).

Python 配置 `GenerationOptions` 和 `run_generation_from_report` 追加了有默认值的 keyword-only 参数 `visual_memory`、`max_model_calls`; 原有位置参数及未启用时的默认行为保持. `run_shader` / `run_generation` 继续通过原来的 `options` 参数接收配置.

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

`workflows/analysis/` 管理固定输入、草稿修订与冻结接受. `agents/analysis/native.py` 装配单个观察角色和受限工具, 草稿文件由 `infrastructure/storage/observation_draft.py` 管理. `domain/scene_observations.py` 校验 E/F/R, 正式包由 `observation_package.py` 发布与读取. SDK 管理模型消息、摘要与外置材料.

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

相对文件路径按 `asset_root` 解析, 默认使用当前工作目录。指定了代码或预览路径但文件不可用时会返回错误; `preview_path=None` 明确表示没有提供预览。该默认 Context Builder 支持 PNG, 不缩放或裁剪图像. 视觉记忆模式通过独立的材料装配接入按需裁剪及历史请求精简, 见[按需视觉记忆](#按需视觉记忆).

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

未绑定报告的任务沿用上述 Context Builder 入口. 报告生成由公开 `run_generation_from_report` 统一准备, 内部每轮复用固定原图、有效方案和基线材料, 并刷新候选与错误; 不把带报告绑定的任务直接交给旧 `run_generation`. 当前分析在 `agents/analysis/` 为单个观察角色构造固定材料与受限工具. 原生分析接受 SDK 的上下文摘要与工具输出外置; 生成不额外调用摘要模型, 保留有限轮数下的完整有效历史. 独立视觉评审通过 `run_review` 接收固定双图和任务标准, 使用专用上下文发布评审包.

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

新观察角色暂不开放裁剪或区域统计工具, 不要求模型维护证据关联. 旧完整五库包的证据读取仍保留, 见[辅助材料契约](docs/analysis-five-libraries.md#2026-10-27-内容收缩与辅助材料契约); 历史取证类型不构成旧分析执行支持.
