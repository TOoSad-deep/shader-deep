# Shader Deep 当前架构

本应用从参考图建立可追溯的分析结果, 或通过实际渲染生成 Shader. 两条流程保持独立, 不自动把分析结果送入生成. 本文描述重构后的实际代码; 历史设计与旧协议文档不替代当前入口.

生成开发的目录、模块和四个实施分支见[生成整体架构与模块设计](generation-architecture.md), 选择理由见[宏观分析](generation-execution-design.md). 输入准备、报告执行、两方案比较及两元素整图组合已接入, 模型视觉效果与用户验收单独记录.

结构选择及取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md); 实现的验证与交付状态见[结构重构任务](work-items/structure-refactor.md).

分析重设计按主题分为两份文档: 调度流程、模块职责和 Agent 编排见[分析架构与 Agent 工作流设计](analysis-architecture-design.md); 五库语义、字段、引用和报告公共状态见[五库与报告数据设计](analysis-five-libraries.md). 两份文档分别记录已确认设计与待讨论方案, 默认五库代码已接入; 主 Agent + skill 的本轮实施契约见[改造方案](work-items/main-agent-skill-phase-one-plan.md), 草图最低产出继续另议. 实施与验证状态见[五库实施记录](work-items/five-library-implementation.md). 本文区分新默认流程和保留的四库兼容链路.

## 从哪里开始阅读

1. [api.py](../src/shader_deep/api.py): 可用的 Python 入口和返回类型.
2. [分析入口](../src/shader_deep/workflows/analysis.py)与[五库协调器](../src/shader_deep/workflows/five_analysis.py): 一轮分析如何创建、运行和结束.
3. [五库角色](../src/shader_deep/agents/five_analysis/agent.py): 目标规划、探索、整合的真实输入、工具与输出.
4. [五库领域入口](../src/shader_deep/domain/five_libraries/__init__.py)、[任务存储](../src/shader_deep/runtime/task_store/store.py)、[文件包](../src/shader_deep/infrastructure/storage/report_package.py): 决定如何经过校验、持久提交和读取.
5. [生成工作流](../src/shader_deep/workflows/generation.py)与[生成角色](../src/shader_deep/agents/generation/agent.py): 候选如何渲染、回读和选择.

## 目录职责

| 目录 | 内容 | 边界 |
| --- | --- | --- |
| `cli/` | 参数、输出、退出码 | 不执行业务校验和调度 |
| `workflows/` | 输入装配、阶段推进、批次调度、进度与交付 | 创建运行及共享资源, 汇总角色结果 |
| `agents/` | 持续主 Agent、独立探索、受限整合、生成及旧角色实现 | 提示词、上下文、工具权限按角色组织 |
| `domain/` | 任务、黑板、证据、五库及四库兼容引用规则 | 不依赖模型框架、Agent 或文件系统适配器 |
| [runtime/](../src/shader_deep/runtime/README.md) | 调用循环、计数、预算、历史、提交与修复 | 不导入具体 Agent 或历史会话 |
| `infrastructure/` | 模型客户端、传输、文件存储、配置读取、追踪 | 将外部资源适配为业务和执行组件所需接口 |
| `imaging/` | 固定图像、裁剪、像素统计、剖面 | 不做模型推理或选择唯一形成机制 |
| `rendering/` | WebGL2 编译、渲染与浏览器生命周期 | 不导入 Agent、黑板和 LangChain |
| `resources/` | 随安装包发布的 YAML | 默认配置不依赖当前目录 |
| [compatibility/](../src/shader_deep/compatibility/README.md) | 旧分析和按需整合的可执行实现 | 仅供显式兼容调用及回归, 当前工作流不依赖它们 |
| `experiments/` | 固定样本和受控清单实验 | 不自动进入完整图片流程 |

顶层 `analysis/`、`context/`、`tools/` 和旧单文件模块保留为明确的导入转发. 新实现只使用上述规范路径. `domain/legacy.py` 保留旧报告及兼容任务字段的数据模型, 不运行旧 Agent.

## 两条当前流程

分析:

```text
CLI / Python API
  → 创建运行并固定参考图
  → 持续主 Agent 发现并读取编排 skill
  → submit_elements 登记元素, 回执进入下一次主模型请求
  → dispatch_exploration 冻结唯一目标与本批方向
  → 最多三个 worker 独立探索 F/R/M/S, 内部完成有限恢复
  → 主 Agent 按需读取结果并调用 dispatch_integration
  → 整合工具汇集、规范化引用并发布不可变 V0
  → 独立线程执行整合 worker, 仅合并 F/R/M
  → 校验通过发布 V1; 失败回退 V0 + partial + gaps
  → 主 Agent 调用 finish_analysis, 程序计算最终状态
  → 完整五库包 + 短入口 + 按草图视图
  → 原子提交包指针并封存, run.json 保存派生投影
```

生成:

```text
CLI / Python API
  → 创建固定目标与任务, 报告模式先捕获输入
  → RenderSession 管理渲染线程与资源
  → 生成角色提交 GLSL
  → 真实渲染与预览回读
  → finish_shader 选择已展示的成功候选, 或 stop_generation 记录主动受阻
  → 返回实际候选与结束原因, 运行异常保存诊断后抛出
```

`workflows/five_analysis.py` 创建运行并提供业务动作, 默认入口运行 `agents/main/` 的持续主 Agent. 主 Agent 按实际读取的编排 skill 调用元素登记、探索派发、结果读取、整合和结束工具, 原固定 `_stages` 不再推进默认链路. 元素登记与探索派发使用请求时工具权限, 同一模型响应中的夹带派发不会因登记刚成功而放行. 每个探索任务绑定同一原图、用户要求、目标和范围, 使用独立模型历史. 一次自动重派仍失败后, 主 agent 独立诊断原任务与自身错误, 只决定最后重派或结束; 决策不改变原输入, 自身失败不递归恢复. 整合拥有独立历史, 仅读已发布 V0; 语义合并只产生 F/R/M 旧项到保留项的映射, 不生成机制或更改草图. 程序一次重写 `[[ID]]` 及结构引用, 对草图选择碰撞、共同机制塌缩和关系端点塌缩拒绝发布.

## 报告生成的准备与执行

[generation_from_report.py](../src/shader_deep/workflows/generation_from_report.py) 的内部 `_prepare_generation` 接收报告、草图、可选备选、本次要求和明确背景, 可从已有黑板选择基线. 它分配一个运行目录, 捕获输入后登记新目标与生成任务, 返回私有 `PreparedGeneration`; 不创建模型客户端、渲染线程或浏览器.

[generation_inputs.py](../src/shader_deep/infrastructure/storage/generation_inputs.py) 复制协议指定文件, 校验副本并计算 manifest、五库与原图的内容 SHA256. `TaskRecord.generation_binding` 默认为空; 新绑定记录副本、摘要与选择, 有效 `selected`、备选条件和公共问题的必要正文单独进入上下文. 基线文件副本的映射写入 `run.json.inputs.baseline`, 历史候选路径与身份保持原值.

固定文本、原图及基线内容由内存材料持有, `_build_generation_context(..., inputs=...)` 和 `GenerationContextMiddleware(..., inputs=...)` 每轮重用它们, 只刷新当前候选与结果. 未绑定任务仍走原路径; 已绑定任务缺少准备材料时明确报错, 不静默丢掉方案. 输入捕获证据见[阶段 01 记录](work-items/generation-01-input-binding.md#实施记录).

公开 `run_generation_from_report` 接收显式选择、背景及逐项渲染配置, 未指定宽高从捕获原图确定. 它将 `PreparedGeneration` 交给共用 `_run_session`, 与旧 PNG 路径使用同一 Agent 和工具循环. `RenderSession` 接收可选 `run_dir`、`inputs`, 复用准备目录并在每次保存中保留基线副本映射. CLI 的 `--report` 模式进入该 API; 原 PNG 模式保持原调用及默认尺寸.

`stop_generation` 经过正常结果引用校验后保存 `blocked` 结果及下一步建议, 无候选也能结束; 成功选择、受阻、逻辑预算耗尽和异常以会话终态统一判断. 同批工具按响应顺序进入单线程, 结束后不再改写候选. 自动模型摘要在应用层关闭; `model_calls` 统计普通逻辑请求, 不包含 SDK 有限网络重试. 执行及资源关闭异常保留 `error` 诊断和目录注释后继续抛出. 验证范围见[阶段 02 记录](work-items/generation-02-execution-loop.md#实施记录).

## 两方案比较

[generation_comparison.py](../src/shader_deep/workflows/generation_comparison.py) 的 `run_generation_comparison` 只接受同一报告中的两项显式 `GenerationPlan`. 先完成两项准备, 第一项捕获原文件, 第二项复用固定报告副本与内存基线; 无效第二项不会在第一项已经调用模型后才暴露. 各自从调用方原始黑板建立新任务, 互不带入对方的新增任务、候选或结论.

比较调用一次 `build_model` 固定客户端配置, 将它交给两个独立 `_run_session`; `_execute` 各自创建 Agent 与消息历史. 只扩展内部模型传参, 旧公开生成入口签名保持不变. 普通运行异常成为该项的错误索引, 另一项继续; 取消则停止批次并保留已有子运行. 没有崩溃恢复或隐式重试整项能力.

[比较存储](../src/shader_deep/infrastructure/storage/generation_comparison.py) 写入 `comparison.json` 与 README 产物导航; [比较领域记录](../src/shader_deep/domain/generation_comparison.py) 只保存共同条件、两项索引和独立人工选择. 读取按调用方给定目录定位; 人工选择核对该项已完成、候选 ID 和子运行当前选择一致, 仅更新比较索引. 子运行内的生成自检与人工选择分开保存. 实施与证据见[阶段 03 记录](work-items/generation-03-plan-comparison.md#实施记录).

## 两元素整图组合

[scene_generation.py](../src/shader_deep/workflows/scene_generation.py) 的 `run_scene_generation` 接收两个 `SceneElementSource` 和整图目标、背景与布局约定. [scene_inputs.py](../src/shader_deep/infrastructure/storage/scene_inputs.py) 从源 `run.json` 核对完成状态、实际已选候选与任务归属, 复用报告捕获规则校验保存的内容身份, 并固定实际代码和可用预览. 两个输入须来自同一完整原图, 报告内短 ID 按各自来源解释.

工作流用全新黑板登记整图目标和任务, 不向 `generation_binding` 塞入多元素来源, 也不登记旧候选. `ScenePlan` 与轻量来源映射随当前 `run.json` 保存. Context Builder 按槽位注入两个有效方案、代码及预览, 完整原图只注入一次, 当前整图候选与错误逐轮刷新. 固定材料在内存中复用, 不回读变化中的来源路径.

`RenderSession` 与上下文中间件的既有 `inputs` 参数接受单方案或整图材料; 继续共用原 Agent、三个工具及单 Pass 渲染契约. 模型可以为组合协调实现, 须在自检中说明偏离; 最终候选只来自新整图会话的实际渲染. 没有新增拼接器、图层合成器或元素子 Agent. 实施与证据见[阶段 04 记录](work-items/generation-04-scene-composition.md#实施记录).

## 状态由谁持有

| 状态 | 唯一责任方 |
| --- | --- |
| 固定目标与公开结果索引 | 工作流维护黑板; `domain/blackboard.py` 校验登记 |
| 五库字段与引用规则 | `domain/five_libraries/`, 不含 IO 或模型框架 |
| 任务、attempt、恢复计数、回执、V0/V1 指针和封存 | `runtime/task_store/` 的 `commit.json`, 每轮进程锁及线程锁 |
| 角色历史与请求级图像材料 | `agents/five_analysis/` 与复用的 `AnalysisLoop` |
| 完整交付文件和按草图读取 | `infrastructure/storage/report_package.py`, 来自同一选定版本 |
| 分析 `run.json` | 公开入口从权威提交记录生成的投影, 不接受提交、不承担恢复指针 |
| 生成 `run.json` | RenderSession 保存黑板、固定输入引用、计数与终态; 不是可恢复的模型会话 |
| `comparison.json` | 顺序比较工作流发布共同条件与子运行索引; 人工选择入口只更新该索引 |

结果文件先写齐、验证, 再原子替换一份提交记录发布任务终态、结果路径与回执. 完成事件只用于唤醒, 协调器补查持久状态. 相同提交成功后重试返回原回执, 终结/取消/替换/封存阻止迟到的新提交. 进程锁及代次管理恢复无执行者的旧 `running`, 保留已提交结果及恢复计数.

## 五库与文件包

数据字段只由[五库数据设计](analysis-five-libraries.md)定义. 草图直接保留全部来源产物, 每套默认及局部备选不被整合 worker 重组. 相似项合并不是简单字符串替换; 引用合法也不证明视觉语义等价. 不变版本 V0 在整合前保存, V1 验证和发布失败时回退到 V0 并标记 partial. 无合法可用基线时明确 failed.

`report/README.md` 和 `manifest.json` 为人工与机器入口, 五个 JSON 分别保存对象数组, 固定原图随包保存. 包可整体移动, 对象短 ID 只在当前包有效. `read_report_package` 校验完整包, `read_sketch` 返回明确范围的上下文视图, 可显式选择一项局部备选; 它保留整轮缺口并不声称自包含导出. 公开黑板结果只记录协议及交付索引, 不再复制完整五库正文.

## 四库兼容链路

`workflows/legacy_analysis.py`、`coordinator.py`、旧 outline/exploration/integration 角色及 `domain/library/` 保留 `possibility_library_v1` 和旧历史回放. [四库模块说明](../src/shader_deep/domain/library/README.md)、[旧整合说明](../src/shader_deep/agents/integration/README.md)描述该链路. 默认五库不消费旧报告, 未进行静默数据转换.

## 配置和依赖

`infrastructure/configuration.py` 只安全读取 YAML. `workflows/configuration.py` 校验允许字段、处理相对路径并应用既有覆盖优先级. 模型地址与凭据仍由外部环境提供, 不进入快照. 模型客户端按 `OPENROUTER_*`、`DS_MICU_*`、`MICU_*` 顺序整组选用; OpenRouter 分析保留推理块的流式汇集与工具历史回传, 不新增独立模型 SDK.

`AnalysisOptions` 保留旧字段, 默认 `max_tasks` 改为 3; 新入口只接受一批 2 或 3 个方向. 主/子总调用上限默认 0, 固定修复/重派次数与无进展规则继续生效. 四库特有比较阶段参数仅由兼容链路使用. 模型传输与请求检查只依赖 `runtime/options.py` 中的最小只读协议, 不需要了解探索数量等业务配置. 生成参数由 `agents/generation/options.py` 管理.

五库角色复用现有模型传输和 AnalysisLoop, 通过独立执行实例及受限工具落实职责. 主 Agent 使用 SDK SkillsMiddleware 发现随包发布的 `resources/skills/analysis-orchestration/SKILL.md`, 通过限定读取工具取得完整方法内容后执行业务动作; 请求材料与工具执行白名单共同限制权限. 探索与整合仍复用原角色提示和独立上下文. 生成和分析保持独立.

## 兼容边界

公开 Python 函数保持参数签名, `AnalysisOutcome` 新增可选 `report_dir`; 分析输出协议和载体切换为五库文件包, 包括 `run_analysis`、`run_analysis_task`、`run_shader`、`run_generation`、`generate_shader` 和 `generate_task`. 旧命令行入口和 `main.py` 继续可用; 新代码推荐从 `shader_deep.api` 导入.

旧导入转发、旧执行流程和旧数据契约的区别, 以及回放与后续删除条件, 集中在[兼容模块说明](../src/shader_deep/compatibility/README.md).
