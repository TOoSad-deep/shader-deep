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

独立调参实验通过调用方管理的 `ShaderPreview` 固定源码和参数白名单, `experiments/tools/` 每个工具独立一个文件, `factory.py` 负责绑定会话、固定参考图与共享历史. 旧 `experiments/parameter_tool.py` 仅转发公开工厂导入. 浏览器验证实际活跃 uniform 类型并通过 `gl.uniform1f` 赋值, 不替换源码, 也不逐次重建浏览器/program. 单帧 `WebGL2Renderer.render(..., uniforms=...)` 仍用于外层生成参考图等场景.

常驻预览由 `rendering/preview.html` 和 `webgl2.js` 中的 `createShaderPreview` 提供, 页面启动时编译一次, 持续复用 program 绘制. `setParameters` 校验并合并参数, `capture` 同步绘制并导出带参数版本的 PNG, `dispose` 停止动画并释放 GPU 资源. 页面支持默认示例或调用方注入的配置, 预览参数更新保留其他当前值. 单帧接口继续在每次调用后释放 program.

`shader-preview-update` 事件传递成功更新后的完整参数快照, 页面统一同步控件. 生命周期根据 `pagehide.persisted` 区分往返缓存暂停和永久关闭; `pageshow.persisted` 仅恢复已暂停且未销毁的会话, 避免重复动画循环.

Python 常驻入口为 `rendering/preview.py` 的 `ShaderPreview`, 从渲染包公开导出. `with` 管理启动/关闭, 一个内部工作线程持有浏览器和页面, 外部线程的参数更新、取图及关闭串行执行. 页面通过启动配置加载调用方源码和参数, 复用同一个已编译 program; `PreviewSnapshot` 保存 PNG、不可变参数视图、参数版本、实际 iTime 和尺寸. 构造不启动资源, 启动失败与关闭都会释放浏览器及线程, 已关闭实例不能重新进入. 当前每个会话使用独立浏览器, 不连接之前手动打开的标签页; 实验 Agent 工具通过同一个会话的更新和取图接口操作预览.

渲染模块内部按职责组织: `models.py` 定义统一类型与异常, `_validation.py` 执行共享校验, `preview.py` 管理公开会话与线程, `_preview_browser.py` 管理浏览器 Adapter 与页面通信. `preview.html` 提供页面结构, `preview.js` 管理控件, `webgl2.js` 管理 GPU 操作. 原 `renderer.RenderError` 导入与公开调用保持兼容, 实验工具从会话的 `parameter_specs` 副本获得参数说明. 具体依赖与接口契约见[渲染模块说明](../src/shader_deep/rendering/README.md).

独立调参实验的 `build_parameter_tools(preview)` 提供 `set_parameters` 与 `capture_preview`, 仅转换模型输入、预期错误和图片消息, 不拥有浏览器或候选文件. `build_parameter_agent(preview=...)` 使用原生 Deep Agents, 可直接调用; 模型客户端沿用项目配置或显式注入, 原单工具参数保留兼容. 调用方在外层 with 内完成模型调用, 更新与取图分轮执行. 此实验不接入默认分析/生成工作流.

新代码统一从 `shader_deep.experiments.tools` 导入工厂. 目录中的 `set_parameters.py`、`capture_preview.py`、`compare_preview.py`、`get_trial_history.py` 分别定义单个模型工具, `_common.py` 共用消息适配与错误处理设置. 拼图和历史存储继续留在图像模块及实验历史模块, 不混入工具定义. 详细职责见[工具 README](../src/shader_deep/experiments/tools/README.md).

参考图匹配任务由 `experiments/parameter_task.py` 的 `build_reference_task` 构建: 本地 PNG 经现有图片消息适配器读成固定 data URL, 随 HumanMessage 传给模型, 不传路径或目标参数. 另用 `build_parameter_agent(preview=..., reference_path=...)` 给工具绑定同一参考图. 消息和工具分别固定参考字节, 构建之间不得修改文件, 换目标时需新建 Agent.

参考图模式新增 `compare_preview` 与 `get_trial_history`: `imaging/comparison.py` 原尺度拼接左参考/右当前并加独立标题栏, 不计算误差; `experiments/_parameter_history.py` 只保存成功取图的原始快照, 工具最多返回三张所选原图与参数. 历史生命周期与本次工具绑定一致, 不跨目标共享, 不自动选优或恢复. 渲染模块不依赖参考图或比较逻辑, 仍只负责当前画布. 模型可见内容无 MAE、评分、排名或程序判断的改善结论; 评估指标仅由外层离线计算. 之前的真实模型实验未使用这些新工具, 新链路的实际精度收益待验证; 历史证据见[实验记录](work-items/parameter-agent-experiment.md).

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

`infrastructure/configuration.py` 只安全读取 YAML. `workflows/configuration.py` 校验允许字段、处理相对路径并应用既有覆盖优先级. 模型地址与凭据仍由外部环境提供, 不进入快照. 模型客户端按 `OPENROUTER_*`、`DS_MICU_*`、`MICU_*` 顺序整组选用; OpenRouter 分析保留推理块的流式汇集与工具历史回传, 不新增独立模型 SDK.

`AnalysisOptions` 保留旧字段, 默认 `max_tasks` 改为 3; 新入口只接受一批 2 或 3 个方向. 主/子总调用上限默认 0, 固定修复/重派次数与无进展规则继续生效. 四库特有比较阶段参数仅由兼容链路使用. 模型传输与请求检查只依赖 `runtime/options.py` 中的最小只读协议, 不需要了解探索数量等业务配置. 生成参数由 `agents/generation/options.py` 管理.

五库角色复用现有模型传输和 AnalysisLoop, 通过独立执行实例及受限工具落实职责. 方法 skill 的具体加载封装仍待确定, 没有将普通提示词文件宣称为已接入 skill 系统. 生成和分析保持独立.

## 兼容边界

公开 Python 函数保持参数签名, `AnalysisOutcome` 新增可选 `report_dir`; 分析输出协议和载体切换为五库文件包, 包括 `run_analysis`、`run_analysis_task`、`run_shader`、`run_generation`、`generate_shader` 和 `generate_task`. 旧命令行入口和 `main.py` 继续可用; 新代码推荐从 `shader_deep.api` 导入.

旧导入转发、旧执行流程和旧数据契约的区别, 以及回放与后续删除条件, 集中在[兼容模块说明](../src/shader_deep/compatibility/README.md).
