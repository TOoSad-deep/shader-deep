# Shader Deep 当前架构

本应用从参考图建立可追溯的分析结果, 或通过实际渲染生成 Shader. 两条流程保持独立, 不自动把分析结果送入生成. 本文描述重构后的实际代码; 历史设计与旧协议文档不替代当前入口.

结构选择及取舍见 [ADR 0001](decisions/0001-agent-oriented-layout.md); 实现的验证与交付状态见[结构重构任务](work-items/structure-refactor.md).

## 从哪里开始阅读

1. [api.py](../src/shader_deep/api.py): 可用的 Python 入口和返回类型.
2. [分析入口](../src/shader_deep/workflows/analysis.py)与[协调器](../src/shader_deep/workflows/coordinator.py): 一轮分析如何创建、运行和结束.
3. [初稿角色](../src/shader_deep/agents/outline/agent.py)、[探索角色](../src/shader_deep/agents/exploration/agent.py)、[整合角色说明](../src/shader_deep/agents/integration/README.md): 每个角色的真实输入、工具与输出.
4. [四库模块说明](../src/shader_deep/domain/library/README.md): 模型决定如何经过程序校验和提交.
5. [生成工作流](../src/shader_deep/workflows/generation.py)与[生成角色](../src/shader_deep/agents/generation/agent.py): 候选如何渲染、回读和选择.

## 目录职责

| 目录 | 内容 | 边界 |
| --- | --- | --- |
| `cli/` | 参数、输出、退出码 | 不执行业务校验和调度 |
| `workflows/` | 输入装配、阶段推进、批次调度、进度与交付 | 创建运行及共享资源, 汇总角色结果 |
| `agents/` | 初稿、探索、整合、生成的角色实现 | 提示词、上下文、工具权限按角色组织 |
| `domain/` | 任务、黑板、初稿、证据、四库与引用规则 | 不依赖模型框架、Agent 或文件系统适配器 |
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
  → OutlineAgent 提交初稿与方向
  → dispatch.run_batch 并行执行独立探索
  → IntegrationSubagent 同步执行整合
  → delivery.finalize_analysis 检查完成义务
  → progress.save_progress 保存主快照
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

`workflows/coordinator.py` 只推进阶段, 不继承历史探索会话. `AnalysisRun` 保存整轮记录; 调度在 `dispatch.py`, 完成条件在 `delivery.py`, 主快照在 `progress.py`.

整合的阶段、工具权限和结果语义见[整合模块说明](../src/shader_deep/agents/integration/README.md); 请求执行、串行工具、重试及历史规则见[运行组件说明](../src/shader_deep/runtime/README.md).

## 状态由谁持有

| 状态 | 唯一责任方 |
| --- | --- |
| 目标、任务、探索报告、候选和结果登记 | 工作流维护黑板; `domain/blackboard.py` 校验登记 |
| 四库版本、来源、别名和幂等回执 | `domain/library/store.py` |
| 某个角色的模型历史、草稿和局部修复 | 对应角色的执行实例和 `runtime` |
| 探索并发、取消、总额度和最终状态 | 工作流 |
| 主 `run.json` | 工作流进度组件 |
| 子角色快照与工具日志的实际写盘 | 存储适配器 |

跨角色通过复制后的业务进度更新主快照, 不共享模型历史或草稿. 进度与共享额度的具体规则集中在[整合模块说明](../src/shader_deep/agents/integration/README.md).

## 四库的更新边界

四库保留统一的校验和版本发布入口, 通过持久化接口适配外部存储. 数据关系、材料呈现、幂等及失败语义集中在[四库模块说明](../src/shader_deep/domain/library/README.md); 模型是否提出正确的机制仍需另行评估.

## 配置和依赖

`infrastructure/configuration.py` 只安全读取 YAML. `workflows/configuration.py` 校验允许字段、处理相对路径并应用既有覆盖优先级. 模型地址与凭据仍由外部环境提供, 不进入快照.

完整流程配置保留 `AnalysisOptions` 的旧字段和默认值. 模型传输与请求检查只依赖 `runtime/options.py` 中的最小只读协议, 不需要了解探索数量等业务配置. 生成参数由 `agents/generation/options.py` 管理.

角色保留原有调用行为. 本次没有强制生成和分析使用相同的循环, 没有新增模型摘要、额外模型请求或自动分析到生成交接.

## 兼容边界

旧 Python 入口保持参数与返回类型, 包括 `run_analysis`、`run_analysis_task`、`run_shader`、`run_generation`、`generate_shader` 和 `generate_task`. 旧命令行入口和 `main.py` 继续可用; 新代码推荐从 `shader_deep.api` 导入.

旧导入转发、旧执行流程和旧数据契约的区别, 以及回放与后续删除条件, 集中在[兼容模块说明](../src/shader_deep/compatibility/README.md).
