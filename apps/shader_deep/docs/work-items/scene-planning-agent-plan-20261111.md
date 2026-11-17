# 单个全图规划 Agent 与生成交接实施方案

日期为 2026-11-11。首版实现与本轮校准已完成，内容语义及视觉偏差见第 13 节。

本方案依据[最新五库设计稿](five-library-planning-contract-20261111.md)，将其落实为单个规划 Agent、文件合同与整图生成入口。业务字段及语义以该设计稿为准。

## 1. 本轮交付目标

接通以下主流程。

```mermaid
flowchart LR
    O[已发布的全图 E/F/R] --> I[固定观察版本、要求与生成约束]
    I --> P[一个规划 Agent]
    P --> D[M/S 草稿与局部修订]
    D --> V[正式规划包]
    V --> C[显式选择 S 与可选的一项备选]
    C --> G[现有生成 Agent]
    G --> R[现有 WebGL2 渲染与预览回读]
    R --> A[代码、预览、来源与剩余缺口]
```

当前 E/F/R 已获用户认可为可用基线。下一阶段从已发布观察包开始，以发光图和磨砂叠圆跑通端到端链路。

规划阶段由一个 Agent 在同一会话中组织 M 和 S。普通任务先形成一套有明确实现安排的整图 S，有实质路线差异时再保留其他 S。方案数量由问题决定。

本轮完成条件是正式规划包能被读取、显式选择的整图方案能进入真实生成请求，并产出可检查的 Shader 与预览。视觉效果由原图对照和人工审阅确认。

## 2. 现状与实际接入点

| 当前能力 | 源码依据 | 本轮处理 |
| --- | --- | --- |
| 一个原生观察图及受限工具 | [build_analysis_agent](../../src/shader_deep/agents/analysis/native.py) | 复用 SDK 组装方式与执行保护 |
| 首次保存、局部编辑、分页回读、接受快照 | [AnalysisSession](../../src/shader_deep/workflows/analysis/session.py)、[DraftStore](../../src/shader_deep/infrastructure/storage/observation_draft.py) | 抽取两个具体角色共用的草稿能力 |
| 独立 E/F/R 发布与读取 | [observation_package.py](../../src/shader_deep/infrastructure/storage/observation_package.py) | 作为规划输入入口 |
| 旧五库机制与单元素草图读取 | [五库模型](../../src/shader_deep/domain/five_libraries/models.py)、[read_sketch](../../src/shader_deep/infrastructure/storage/report_package.py) | 新规划使用独立合同及读取入口 |
| 旧报告生成任务绑定单元素 | [GenerationBinding](../../src/shader_deep/domain/generation.py)、[generation_from_report.py](../../src/shader_deep/workflows/generation_from_report.py) | 新增整图规划绑定与准备分支 |
| 生成循环与真实渲染 | [_run_session](../../src/shader_deep/workflows/generation.py)、[RenderSession](../../src/shader_deep/agents/generation/tools/session.py) | 接收新的固定方案材料 |

两个接入差异需要明确处理。

- 观察 manifest 保存 context_notes、open_questions 和 gaps，原始用户要求需要由规划调用方显式传入。
- 旧生成绑定的 element_id 为必填，任务正文也限定单元素。新整图入口以完整观察范围绑定任务。

## 3. 运行与状态归属

| 层 | 责任 |
| --- | --- |
| domain/scene_planning.py | 新提交类型、引用与覆盖规则、生效方案和候选索引派生 |
| infrastructure/storage | 观察捕获、内容身份、草稿字节、正式包发布与读取 |
| workflows/planning | 创建运行、固定输入、调用一次原生图、接受快照和交付 |
| agents/planning | 角色规则、实际请求上下文、工具白名单与 SDK 装配 |
| generation_from_planning | 捕获选中方案、登记整图生成任务并调用既有生成执行 |

PlanningSession 管理本次固定输入、草稿版本、调用计数、接受快照和终态。SDK 管理消息、摘要和会话内文件状态。

规划运行直接使用独立 run 目录与 PlanningSession。生成任务在交接时登记到现有黑板，包含来源规划身份与显式选择。

## 4. 固定输入与版本绑定

### 4.1 规划入口

建议新增以下同步公开入口，参数名称在实施时核对包内风格。

```python
# 新增接口草案
run_planning(observation_dir, request, *, options=None, generation_options=None)
read_planning_package(directory)
read_planning_sketch(directory, sketch_id, *, alternative_index=None)
run_generation_from_planning(
    planning_dir, *, sketch_id, alternative_index=None, options=None
)
```

request 显式表达本次整图目标。PlanningOptions 管理规划调用预算和传输配置。generation_options 提供计划采用的渲染尺寸与时间，省略时沿用报告生成的原图尺寸约定。

CLI 增加 shader-deep-plan，接收观察报告目录与要求。生成命令新增显式 --planning-report 模式，配合 --sketch 和可选 --alternative。备选索引沿用现有零基约定。

生成入口的 options 省略时，从规划快照继承 width、height、time，再设置生成阶段预算。显式传入 GenerationOptions 时，其渲染字段作为本轮执行条件，差异写入运行记录并随规划假设一起进入生成请求。

CLI 用未设置的参数保留继承语义，仅修改 --max-attempts 时继续使用规划尺寸和时间。直接 API 调用按完整 options 对象处理，调用方调整预算时可基于规划条件构造该对象。生成准备先固定实际条件，后续候选共用同一条件。

### 4.2 捕获与身份

运行准备阶段捕获观察正式文件，再对捕获副本调用正式读取校验。后续模型请求、领域校验和最终发布共用该副本。

观察业务内容身份覆盖下列实际字节。

```text
manifest.json
elements.json
features.json
relations.json
reference.png
```

摘要沿用现有内容身份的文件名、长度和字节边界编码。原图摘要单独保留。README 作为导航随包保存，观察业务身份由上述五个文件决定。

原图相同而 E/F/R、上下文或 gaps 改变时，观察内容身份随之改变。采用修订观察时创建新规划运行。

规划输入另固定 request 和 generation_constraints.json。约束来自当前真实生成与渲染合同，包含语言、入口、Pass、可用输入及渲染条件。

首版以当前单 Pass GLSL ES 3.00 mainImage 环境为基线。底层颜色的解析求值可在单 Pass 内安排，纹理或额外 Pass 的可用性按实际环境表达。

生成输入适配在执行前核对明确的能力冲突，并将具体差异交付给调用方。生成次数和网络重试预算由各阶段分别管理。

## 5. 领域合同与程序派生

模型只提交 mechanisms、sketches 和可选 extra_candidates。新协议名建议为 scene_planning_v1。

实施时从业务设计稿落实以下类型。

| 类型 | 用途 |
| --- | --- |
| PlanningMechanism | 自包含的方法与内在 conditions |
| PlanningSketch | 整图 default、composition、gaps 和 alternatives |
| PlanningGap | 非空 F/R refs 与具体原因 |
| PlanningAlternative | 替换已有选择的 choices 及必要说明 |
| ExtraCandidate | 尚未进入方案的目标与共同机制组合 |
| ScenePlanningSubmission | 模型首次保存的 M/S 正文 |

### 5.1 正式校验

正式接受前校验所有 S 的默认视图及每项备选的完整生效视图。

- 检查 ID、字段、非空文本、可选数组、唯一性和引用闭合。
- 检查 default 的目标来自绑定观察中的 F/R。
- 检查遗漏目标有具体 gaps，已有映射同时存在的 gaps 继续保留。
- 检查备选只替换默认已有目标，每个值替换整组 M。
- 解析生效 composition，检查显式 [[ID]] 标记及当前所选 M。
- 检查 M.method/conditions 的自包含性，发现业务 ID 标记时返回具体位置。
- 检查所有正式 M 均有默认、备选或补充候选来源。
- 检查相同目标与共同 M 集合的重复补充候选，返回可定位问题。

普通变量按文本处理。正文中跨对象引用使用 [[ID]]，旧观察正文直接继承。

程序反馈 code、path、message 与必要的 related_paths。方法可行性、条件是否真正满足、跨元素配合和候选限制迁移，由规划 Agent 复核并通过生成验证。

### 5.2 双来源缺口

每套生效方案的完整性共同依赖绑定观察 gaps 与 S.gaps。两类内容保留各自来源。

```text
T = 本轮全部 F/R
D = 生效 choices 的目标
G = S.gaps.refs 的并集
U = 固定观察 manifest.gaps

D ⊆ T
G ⊆ T
T - D ⊆ G

无已知必要缺口 = U 为空 且 S.gaps 为空 且 T-D 为空
```

E 级和全图级 U 原样进入规划及生成上下文。open_questions 保留为待确认材料。规划只能通过绑定修订后的观察版本更新 U。

有缺口的合法规划可以发布并用于暂定实现。选中方案的两类缺口持续进入生成材料与最终交付，生成会话按实际情况继续或 stop_generation。

建议正式包逐 S 保存派生完整性。汇总 status 在至少一套 S 无已知必要缺口时为 completed，其余合法发布为 partial。选择具体 S 时始终读取该 S 的完整性。

纯背景合法输入支持空 M、空 default 与非空 composition，沿用相同的 U 检查。

### 5.3 生效方案与候选索引

read_planning_package 的 source 提供完整 E/F/R、原图与共同输入。read_planning_sketch 返回选中 M、生效 choices、composition、方法与备选 conditions，以及两类缺口。生成捕获层将二者固定为同一版本的请求材料。

候选索引由每个默认视图、每项备选的完整生效视图和 extra_candidates 派生。每行保存 target、共同 mechanisms 与 origins。

origin 定位 sketch_id 及可选 alternative_index，或 extra_candidate_index。相同目标与 M 集合可合并展示，所有来源继续保留。条件、理由、组织说明和缺口从来源对象解析。

候选转入 S 时，Agent 先将仍有效的限制落实到 composition 或 gaps，再移除新版本中的重复 extra_candidates。程序反馈组合重复，源版本和修改历史保留迁移依据。

## 6. 单个规划 Agent 的 harness

### 6.1 工具

| 工具 | 行为 |
| --- | --- |
| save_planning_draft(report) | 首次创建 /draft/planning.json，返回版本和诊断 |
| read_file | 读取固定材料、规则参考、SDK 外置历史及草稿 |
| edit_file | 对 /draft/planning.json 做唯一匹配的局部修改 |
| finish_planning() | 校验并冻结最新已完整回读的草稿 |
| stop_planning(reason) | 保存具体受阻原因与现有草稿 |

可见工具和实际执行端均实施白名单。输入与规则为只读文件，规划草稿为唯一可编辑文件。

沿用现有局部修订行为。

- 字段合法的初稿可以带跨条目引用问题，工具反馈具体位置。
- 同批工具按声明顺序执行，每次编辑基于前一步已提交版本。
- 修改后完整回读最新版本，分页覆盖同一版本的全部内容。
- 回读正文实际进入后续模型请求后，finish 才能接受。
- 预算耗尽保留草稿及失败原因，接受快照只在 finish 成功后产生。

### 6.2 上下文与记忆

| 材料 | 管理方式 |
| --- | --- |
| 原图、完整 E/F/R、要求与生成约束 | 固定捕获，请求级注入每次实际主模型请求 |
| 主流程 skill | 专用角色 system prompt |
| 复杂判断参考 | SDK StateBackend 只读文件，按条件 read_file |
| 模型会话历史与摘要 | SDK 管理 |
| 最新规划正文 | 磁盘草稿，版本由实际字节摘要标识 |
| 已见回执 | 保存版本、正文摘要和行覆盖范围 |
| 跨运行复用材料 | 调用方显式传入已发布规划及其来源 |

首版记忆由固定输入、当前草稿与 SDK 会话历史构成。跨图机制检索和长期经验写回在实际出现复用需求后设计。

### 6.3 渐进式披露

主 SKILL.md 保留所有任务都要执行的步骤。

1. 阅读固定目标、观察和能力限制。
2. 组织方法及整图方案，核对必要交互与条件来源。
3. 保存初稿，回读并局部修订。
4. 核对覆盖、缺口与生效组合后完成。

主文件同时说明 M 的最小信息、default/composition 分工、双来源缺口和局部修改入口。复杂分支放入参考文件。

| 仍未解决的判断 | 参考文件 |
| --- | --- |
| 方法应拆分还是复用，输入依赖归属不清 | references/mechanisms.md |
| 需要局部替换，或候选转入方案时限制如何保留 | references/alternatives.md |
| 必要条件来源、实现缺口或上游缺口的影响仍不明确 | references/conditions-and-gaps.md |

参考采用简短的通用边界例子。完整磨砂样例留作离线夹具与人工核对材料。

### 6.4 预算、追踪与故障

建议规划初始 max_model_calls 为 20，可由显式配置覆盖。该值用于首轮运行控制，后续根据实际调用与修订收益调整。

传输复用现有模型选择、超时、有限重试和响应校验。记录实际模型、推理配置、规则资源哈希、逻辑调用、token usage、工具拒绝与错误位置。

LangSmith 沿用项目追踪配置。规划运行、源观察身份、生成子运行及选中 S 在本地索引和追踪 metadata 中关联。

最终交付保留网络失败、未接受草稿、正式发布失败和主动受阻的实际状态。模型文本自称完成时仍由会话检查接受快照。

## 7. 运行目录与文件内容

建议采用以下结构。

```text
run-*/
  inputs/
    observation/                 固定观察包
    request.txt                  本次规划要求
    generation_constraints.json  实际生成约束
    binding.json                 内容身份及来源
  draft/
    planning.json                模型唯一编辑的 M/S 正文
    history/<revision>.json      已提交旧版本
  results/
    planning.json                接受时冻结的正文
  report/
    manifest.json                scene_planning_v1 及文件摘要
    observation/                 可独立读取的固定观察包
    request.txt
    generation_constraints.json
    mechanisms.json
    sketches.json
    extra_candidates.json
    candidates.json              程序派生
    README.md                    五库导航及缺口说明
  manifest.json                  运行、配置、计数和终态
  result.json                    对外结果索引
```

正式 manifest 绑定观察内容身份、要求、约束和规划正文摘要。逐 S 完整性与候选索引由 reader 复算并核对。

report 内的 observation 为发布时复制的固定输入，支持整个 report 迁移目录后独立读取。观察正文由源副本提供，模型提交始终只维护 M/S。

发布在独立暂存目录写入并执行正式 reader 校验，再将目录发布到新的 report 位置。运行结果只有在发布成功后登记包路径。已有接受正文在发布失败时继续保留。

## 8. 实现文件与复用尺度

以下路径均相对于 src/shader_deep，表示建议的职责边界。

```text
domain/
  scene_planning.py              新领域合同与纯派生
  generation.py                  整图规划绑定
  tasks.py                       任务绑定的精确类型扩展
infrastructure/storage/
  json_draft.py                  共享 JSON 字节、版本与局部编辑
  observation_draft.py           观察校验适配
  planning_draft.py              规划校验适配
  planning_inputs.py             观察、要求及约束捕获
  planning_package.py            新包发布、读取与方案取材
  scene_inputs.py                元素素材入口识别整图绑定
agents/
  draft_runtime.py               共用草稿后端、顺序执行与回读辅助
  analysis/                     适配共用能力
  planning/
    native.py                    规划专属工具、规则与上下文
    README.md
  generation/
    context.py                   整图规划材料分支
    tools/session.py             新捕获类型的会话传递
workflows/
  planning/
    entry.py
    session.py
    contracts.py
    README.md
  generation.py                  共用执行函数的输入类型扩展
  generation_from_planning.py    新整图准备与生成入口
cli/
  planning.py
resources/
  planning.yaml
  skills/scene-planner/
    SKILL.md
    references/
```

现有 DraftStore、DraftBackend 与 ObservationGuard 绑定观察类型及路径。共享提取聚焦 JSON 草稿 IO 和 SDK 草稿运行辅助，两类角色各自负责领域诊断、接受对象、上下文及交付。

共享修改通过原观察行为测试回归。新增类型遵循现有依赖方向，domain 保持无 IO，rendering 继续独立。

公开导出、CLI 配置和 package-data 在同轮接入。新增 planning.yaml 与 skill 需要从 wheel 检查实际包含情况。

## 9. 整图生成交接

新增 ScenePlanningBinding，保存协议标识、规划内容身份、观察内容身份、sketch_id 与可选 alternative_index。TaskRecord.generation_binding 的类型扩展为 GenerationBinding、ScenePlanningBinding 或 None，默认值继续为空。

validate_generation_task 根据具体绑定类型校验字段。新绑定以完整观察身份确定范围。旧绑定按原字段解释，既有元素素材读取入口识别绑定类别后给出对应处理。

新增 CapturedPlanningInputs 承载正式 reader 解析的有效方案、固定原图和执行条件。_run_session、RenderSession 与 generation/context 的输入类型显式包含该类型。

context._validate_inputs 校验捕获材料与任务绑定一致。上下文、图片与基线装配按捕获类型分支，规划分支始终提供整图材料。各分支测试验证实际请求，覆盖对象属性与序列化读取。

生成上下文包含整图要求、原图、完整 E/F/R、选中 M 的 method/conditions、生效 choices、composition、备选条件、上游观察 gaps 和 S.gaps。

原图默认尺寸、渲染合同、render_shader、预览回读、finish_shader 与 stop_generation 继续由现有生成会话执行。生成结果保存来源身份及实际选择。

选中 S 带有必要缺口时，交付同时呈现这些缺口和实际候选。已渲染、模型自检和用户视觉验收分别记录。

## 10. 实施顺序与阶段验收

| 阶段 | 交付 | 验收证据 |
| --- | --- | --- |
| A 领域与文件合同 | 新提交类型、固定输入、新包 reader/writer、生效方案和候选派生 | 业务设计稿样例经正式读取；关键反例得到可定位诊断 |
| B 规划 Agent | 草稿工具、局部修订、渐进式 skill、配置、API 与 CLI | 无网络模型通过正常入口保存、编辑、回读、接受；原观察回归通过 |
| C 整图生成接线 | 新绑定、固定生成材料、既有循环适配 | 实际模型请求包含选中整图方案；模拟模型配真实 WebGL2 得到候选 |
| D 真模型与视觉核对 | 两图端到端，随后扩展到已有十图的规划回归 | 本地制品、追踪、原图对照及问题归因可对应 |

A 中领域合同与 B 的共享草稿提取可并行。规划角色组装等待领域及草稿接口稳定。C 的输入适配可与 B 的角色实现并行，最后联合验证。

使用 subagents 时按文件所有权分配。

- 领域与存储负责 scene_planning、planning_inputs、planning_package 及对应测试。
- 草稿基础负责共用 JSON/SDK 草稿能力、两个草稿适配及观察回归。
- 规划角色负责 planning 工作流、native 角色、skill 与对应测试。
- 主 Agent 负责公开 API、CLI 汇总接线、生成适配、配置打包和最终验证。

## 11. 必要验证

### 11.1 无网络行为检查

| 范围 | 必要场景 |
| --- | --- |
| 覆盖与缺口 | 漏目标拒绝、有具体 gap 接受、已有映射仍保留 gap、全覆盖仍受上游 E 级或全图 gaps 约束 |
| 特殊输入 | 纯背景空目标、空 M、空 default，上游缺口存在时保留暂定状态 |
| 生效备选 | 整组替换、未改目标继承、非法新增目标拒绝、失效 composition 标记拒绝 |
| 候选来源 | 组合换序比较、来源完整保留、补充项重复可定位、孤立 M 拒绝 |
| 固定输入 | 同图不同观察得到不同身份、源目录后续变化不改变运行、迁移后读取、篡改与缺文件拒绝 |
| 草稿执行 | 唯一编辑、过期版本、同批连续编辑、分页页头、完整回读实际入模、终态后拒绝 |
| 正常入口 | API/CLI 可达、预算耗尽、主动停止、发布失败及配置解析 |
| 下游请求 | 整图 R、选中组合、条件及两类 gaps 实际进入请求；新旧捕获类型通过各自正常入口 |
| 渲染条件 | 规划非默认尺寸与非零时间在缺省生成时继承；CLI 只改预算继续继承；显式渲染覆盖保留差异并实际入模 |

测试覆盖可观察行为。候选限制是否已经在自然语言中落实，使用人工 review 与生成结果核对。

### 11.2 真模型验证顺序

先使用已发布的 buffer-bloom 观察包，核对三个发光元素的 M 复用、光晕组织、整图位置及真实渲染。

随后使用 supah-frosted-glass 观察包，核对共享底层颜色、前圆区域处理与跨元素 R。重点检查所选方法能在实际单 Pass 环境内获得必要输入。

两图通过主流程后，复用已有十图观察产物做规划回归，按覆盖、方法清晰度、交互安排、条件与缺口、调用成本审阅。已有观察的具体问题随输入记录。

真实运行使用已授权的项目模型及 LangSmith 目的地，记录实际模型和推理参数。追加数据目的地或数据范围时按实际变化处理授权。

### 11.3 工程检查

在 apps/shader_deep 中执行 make check。接入生成后执行 make integration_test。新增包资源后构建 wheel 并运行 scripts/check_distribution.py。

实施记录分别保留结构测试、工具执行、真实模型、真实渲染和人工视觉结论。

## 12. 本次规划记录

本轮新增本实施方案并更新路线导航。预算 20、新协议名称、目录布局、汇总状态和接口名称属于本方案的实施建议。

当前运行时代码仍以现有观察及生成入口为准。阶段 A 是下一项具体实施工作，先用设计稿中的完整磨砂样例落实领域与正式 reader，再接规划 Agent。

### 12.1 实施前 review

独立 review 发现两处接线遗漏，已补入第 4、8、9、11 节。

- 生成缺省继承规划尺寸与时间，CLI 预算覆盖保留继承。显式渲染覆盖固定为本轮执行条件并记录差异。
- 明确 ScenePlanningBinding、TaskRecord 类型并集、CapturedPlanningInputs，以及上下文和生成会话的传递分支。

方法自包含的标记校验也纳入正式接受清单。该检查配合模型语义 review 使用。

复核已确认两项接线问题解决，实施方案可以进入阶段 A。文档链接及依赖边界检查通过，结果为 0 findings。本轮验证覆盖方案文档。

## 13. 实施记录

三个 subagents 分别完成领域与存储、共用草稿能力、规划角色及工作流。主任务完成整图绑定、生成上下文、公开 API/CLI、打包与联合检查。独立只读 review 已核对合同、草稿回执、规划 harness 及生成接线。

新增 scene_planning_v1 正式包、PlanningOptions/PlanningOutcome、run_planning、read_planning_package、read_planning_sketch 与 run_generation_from_planning。TaskRecord.generation_binding 接受 ScenePlanningBinding，生成上下文显式处理 CapturedPlanningInputs。

正式规划工具支持首次保存、分页回读、局部修改和冻结接受。观察及规划共用 JsonDraftStore 与 DraftToolGuard/Backend/ReadTracker。两类必要 gaps 在生成输入及最终选择结果中保留，带缺口的选择结果登记为 partial。

真实首次调用发现备选方案在 Python 默认 dump 时产生 Pydantic 序列化警告。根因是 serializer 的返回注解将 tuple 按 JsonValue 重复序列化。已修复返回类型，并补 Python dump、JSON dump、JSON 字符串及实际工具参数 dump 回归。

首个真实产物 review 发现 M 正文借用本轮机制 ID，以及备选 replacement 正文仅写局部补充。主 skill 的复核步骤已补充逐 M 独立阅读、逐备选完整生效组织核对，以及明确的完成判据。局部变量继续按方法定义解释。资源版本在每次运行中固定并记录，回归按实际 bundle 区分初版与修订版。

### 13.1 当前工程验证

- make check 通过，377 项无网络单元测试，Ruff、格式、ty 及仓库检查通过。
- make integration_test 通过，浏览器探针 1 项，完整集成套件 22 项。
- wheel 构建及独立解包检查通过，覆盖资源、导入、公开 API 与 6 个 CLI 模块。

浏览器受限运行在初始化阶段遇到 kill EPERM / SIGABRT。沙箱外同一测试命令通过，Chromium 自身沙箱保持开启。原始失败与成功日志均保存在本次验证记录中。

uv lock --check 暴露既有版本漂移。HEAD 的 SDK 声明为 deepagents 0.7.14，应用锁文件仍记录 0.7.13。dry-run 同时提出六个包的版本更新。当前验证使用已安装环境，此项单独记录为锁文件一致性缺口。

### 13.2 真实校准

真实测试使用已授权 OpenRouter 模型和 LangSmith shader-deep 项目。规划预算为 20 次逻辑主调用，实际 reasoning_effort 未设置时记录为 null，后续四个 case 补测显式发送 low。

产物集中于仓库 [runs/scene-planning-implementation-20261111](../../../../runs/scene-planning-implementation-20261111/README.md)。18 次规划尝试保留了 9 completed、2 partial 和 7 failed。最终十个唯一 case 的选定结果为 8 completed、2 partial，全部通过正式 reader、接受快照对照和已见版本检查。

初始输出上限 163,840 允许 Windows 样本报告约 136,098 输出 token 与 1,813 秒。规划默认已收口为 32,768，CLI 实际请求默认与显式覆盖均有回归。部分提供方默认推理在首次响应耗尽该预算，截断工具参数未执行。后续使用现有进程参数显式 low 补测，实际设置逐运行记录。启动示例见应用 README。

四次生成试验保留了 3 completed 和 1 blocked。初版发光图与磨砂叠圆各选出真实渲染候选。修订版发光图首次三次编译失败后受阻，另建运行将渲染预算临时设为 6，实际第 5 次尝试选出候选。该重试复用相同规划版本。

LangSmith 核对覆盖 18 个规划校准根轨迹与 1 个独立生成重试根轨迹。成功主请求包含原图，观察/规划内容身份与生成选择可关联。网络重试、校准 harness 中断和工具拒绝分别保留。

### 13.3 内容复核与剩余边界

产物对照集中于[复核报告](../../../../runs/scene-planning-implementation-20261111/artifact-review.md)。结构合法、接受回读、实际编译和模型自评各自保存证据。

- 发光图初版有星形与心形距离函数、坐标原点导致的形状与裁切问题。修订后的生成候选可辨认主要形状，仍有内部明暗、光晕与色彩差异。
- 磨砂观察误将前圆描述为比背景亮，并扩大羽化范围，规划继承了该判断，生成继续调整位置。前圆实体感与原图有明显偏差。
- M 自包含在真实报告中仍不稳定，部分方法和 conditions 引用本轮其他方法。部分 partial gaps 混入数值拟合问题，必要性需要澄清。
- Cool-S 的两个真实备选仍依赖默认或另一 S 的正文，且有 smooth-min 与方法条件、条纹算法与 M 定义的矛盾。正文独立取材与机制语义一致性属于当前未通过的内容项。

本轮采用单个规划角色、完整 M/S 初稿及局部修订，固定全图观察与显式 S 选择。方法复用与自然语言可执行性的语义质量继续以这些真实问题为依据迭代。
