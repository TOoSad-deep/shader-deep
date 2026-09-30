# Shader Deep 当前架构

本应用从参考图建立可追溯的分析结果, 或通过实际渲染生成 Shader. 两条流程保持独立, 不自动把分析结果送入生成. 本文描述重构后的实际代码; 历史设计与旧协议文档不替代当前入口.

结构选择及取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md); 实现的验证与交付状态见[结构重构任务](work-items/structure-refactor.md).

分析重设计按主题分为两份文档: 调度流程、模块职责和 Agent 编排见[分析架构与 Agent 工作流设计](analysis-architecture-design.md); 五库语义、字段、引用和报告公共状态见[五库与报告数据设计](analysis-five-libraries.md). 两份文档分别记录已确认设计与待讨论方案, 默认五库代码已接入; 草图最低产出和 skill 具体封装仍待讨论. 实施与验证状态见[五库实施记录](work-items/five-library-implementation.md). 本文区分新默认流程和保留的四库兼容链路.

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
| `agents/` | 目标规划、独立探索、受限整合、生成及旧角色实现 | 提示词、上下文、工具权限按角色组织 |
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
  → 主 Agent 提交元素、唯一目标与单批方向
  → 最多三个 worker 独立探索 F/R/M/S
  → 程序汇集、规范化引用并发布不可变 V0
  → 独立线程执行整合 worker, 仅合并 F/R/M
  → 校验通过发布 V1; 失败回退 V0 + partial + gaps
  → 完整五库包 + 短入口 + 按草图视图
  → 原子提交包指针并封存, run.json 保存派生投影
```

生成:

```text
CLI / Python API
  → 创建固定目标与任务
  → RenderSession 管理渲染线程与资源
  → 生成角色提交 GLSL
  → 真实渲染与预览回读
  → finish_shader 选择本轮已展示的成功候选
  → 返回所选文件中的实际代码
```

`workflows/five_analysis.py` 组织目标规划、单批探索、异步整合及完成判定. 每个探索任务绑定同一原图、用户要求、目标和范围, 使用独立模型历史. 一次自动重派仍失败后, 主 agent 独立诊断原任务与自身错误, 只决定最后重派或结束; 决策不改变原输入, 自身失败不递归恢复. 整合拥有独立历史, 仅读已发布 V0; 语义合并只产生 F/R/M 旧项到保留项的映射, 不生成机制或更改草图. 程序一次重写 `[[ID]]` 及结构引用, 对草图选择碰撞、共同机制塌缩和关系端点塌缩拒绝发布.

## 状态由谁持有

| 状态 | 唯一责任方 |
| --- | --- |
| 固定目标与公开结果索引 | 工作流维护黑板; `domain/blackboard.py` 校验登记 |
| 五库字段与引用规则 | `domain/five_libraries/`, 不含 IO 或模型框架 |
| 任务、attempt、恢复计数、回执、V0/V1 指针和封存 | `runtime/task_store/` 的 `commit.json`, 每轮进程锁及线程锁 |
| 角色历史与请求级图像材料 | `agents/five_analysis/` 与复用的 `AnalysisLoop` |
| 完整交付文件和按草图读取 | `infrastructure/storage/report_package.py`, 来自同一选定版本 |
| `run.json` | 公开入口从权威提交记录生成的投影, 不接受提交、不承担恢复指针 |

结果文件先写齐、验证, 再原子替换一份提交记录发布任务终态、结果路径与回执. 完成事件只用于唤醒, 协调器补查持久状态. 相同提交成功后重试返回原回执, 终结/取消/替换/封存阻止迟到的新提交. 进程锁及代次管理恢复无执行者的旧 `running`, 保留已提交结果及恢复计数.

## 五库与文件包

数据字段只由[五库数据设计](analysis-five-libraries.md)定义. 草图直接保留全部来源产物, 每套默认及局部备选不被整合 worker 重组. 相似项合并不是简单字符串替换; 引用合法也不证明视觉语义等价. 不变版本 V0 在整合前保存, V1 验证和发布失败时回退到 V0 并标记 partial. 无合法可用基线时明确 failed.

`report/README.md` 和 `manifest.json` 为人工与机器入口, 五个 JSON 分别保存对象数组, 固定原图随包保存. 包可整体移动, 对象短 ID 只在当前包有效. `read_report_package` 校验完整包, `read_sketch` 返回明确范围的上下文视图, 可显式选择一项局部备选; 它保留整轮缺口并不声称自包含导出. 公开黑板结果只记录协议及交付索引, 不再复制完整五库正文.

## 四库兼容链路

`workflows/legacy_analysis.py`、`coordinator.py`、旧 outline/exploration/integration 角色及 `domain/library/` 保留 `possibility_library_v1` 和旧历史回放. [四库模块说明](../src/shader_deep/domain/library/README.md)、[旧整合说明](../src/shader_deep/agents/integration/README.md)描述该链路. 默认五库不消费旧报告, 未进行静默数据转换.

## 配置和依赖

`infrastructure/configuration.py` 只安全读取 YAML. `workflows/configuration.py` 校验允许字段、处理相对路径并应用既有覆盖优先级. 模型地址与凭据仍由外部环境提供, 不进入快照.

`AnalysisOptions` 保留旧字段, 默认 `max_tasks` 改为 3; 新入口只接受一批 2 或 3 个方向. 主/子总调用上限默认 0, 固定修复/重派次数与无进展规则继续生效. 四库特有比较阶段参数仅由兼容链路使用. 模型传输与请求检查只依赖 `runtime/options.py` 中的最小只读协议, 不需要了解探索数量等业务配置. 生成参数由 `agents/generation/options.py` 管理.

五库角色复用现有模型传输和 AnalysisLoop, 通过独立执行实例及受限工具落实职责. 方法 skill 的具体加载封装仍待确定, 没有将普通提示词文件宣称为已接入 skill 系统. 生成和分析保持独立.

## 兼容边界

公开 Python 函数保持参数签名, `AnalysisOutcome` 新增可选 `report_dir`; 分析输出协议和载体切换为五库文件包, 包括 `run_analysis`、`run_analysis_task`、`run_shader`、`run_generation`、`generate_shader` 和 `generate_task`. 旧命令行入口和 `main.py` 继续可用; 新代码推荐从 `shader_deep.api` 导入.

旧导入转发、旧执行流程和旧数据契约的区别, 以及回放与后续删除条件, 集中在[兼容模块说明](../src/shader_deep/compatibility/README.md).
