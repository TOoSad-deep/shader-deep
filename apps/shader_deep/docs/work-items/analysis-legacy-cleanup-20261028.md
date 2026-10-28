# 旧分析执行与回放清理

- 日期: 2026-10-28.
- 授权: 用户明确要求删除全部已被原生流程替代的分析执行和回放, 同时保留当前能力、公开入口与历史运行制品.
- 目的: 当前角色与工具从原生 Deep Agents 定义直接阅读, 减少双套执行、转发及历史运行协议的维护成本.

## 删除范围

| 范围 | 处理 |
| --- | --- |
| 四库与固定五库旧执行 | 删除 `compatibility/`、旧角色目录及旧工作流协调、派发、交付入口 |
| 自建分析运行层 | 删除 `runtime/` 的循环、TaskStore、attempt/恢复、共享额度、历史整理与提交修复; 当前仍需的小型配置/严格 JSON 逻辑归入模型基础设施 |
| 回放及探索实验 | 删除 replay CLI、工作流、`experiments/`、旧分析 skill 与 worker skill 资源 |
| 专属领域/存储执行 | 删除旧 LibraryStore、材料门禁、查询/合并/发布执行、旧库日志适配及专属测试 |
| 维护文档 | 当前架构、README、AGENTS 和 ADR 收敛为原生流程; 历史工作记录保留结果, 已删源码链接改为明确的历史路径文本 |

源码删除按调用关系处理, 不以保留空模块或假实现维持旧目录. 历史取舍与当时验证保留在[目录整理记录](analysis-architecture-refactor-plan-20261028.md)和[原生切换记录](deep-agents-native-plan-20261028.md); 其中“旧实现暂留兼容”已由本次授权替代.

## 保留边界

- 当前原生主/探索/整合角色、task 输入隔离和业务提交, 五库模型/引用/证据/报告包及读取入口.
- 当前生成、渲染、单方案/比较/整图组合和本地网页能力; 保留既有未提交的生成改动.
- 公开分析与生成 API、当前 CLI、`main.py`, 及仍被当前消费者使用的兼容包装.
- 历史模型与无 IO 校验仅按实际数据引用保留, 不构成旧执行支持. 四库报告不自动转换或被五库读取器接受.
- 所有已保存 runs、报告、图像及日志保持原样. 本次不读取 `1.txt` 或真实 `.env`, 不调用真实模型, 不提交或推送 Git.

## 验证

本轮使用三个 GPT-6.1 Sol 子 agent 分别清理源码、迁移有效测试和整理文档, 主任务合并公开配置与检查脚本并执行统一验证.

- 对本轮开始时的快照统计: `src/` 删除 185 个源码/资源文件, 新增 1 个统一模型配置模块, 文件数由 291 降至 107. `tests/` 删除 44 个旧文件、增加/迁入 7 个文件; 退役执行专属用例移除, 25 项仍适用的合同迁至当前观察、报告证据、测量和配置接口.
- `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check`: 一次完整运行通过. 226 项无网络单元测试通过, Ruff、146 个 Python 文件格式、ty 与仓库链接/依赖检查全部通过. 未增加重复基准或结构镜像测试.
- `uv build --offline --out-dir /private/tmp/shader-cleanup-dist` 与 `check_distribution.py`: wheel/sdist 构建成功, 原生 skills、导入隔离、公开 API 和 5 个当前 CLI 入口通过. 单独检查 wheel 内无旧 runtime/compatibility/experiments/analysis 包及 replay CLI.
- 移植证据测试发现当前 `ReferenceObservations.snapshot` 直接使用 dataclass 字典时, tuple 无法经过 JsonValue 校验. 已改为 Pydantic 的 JSON 模式序列化, 原图证据保存、移动及生成取材合同通过.
- 当前保留 19 项浏览器集成测试, 本轮未重跑: 生成、渲染和网页实现与清理前快照逐文件比较均未改变. 本轮不调用真实模型, 不声称解决上一轮真实模型未产出合法报告的问题.
- `git diff --check` 通过. 未提交或推送 Git.

验证日志、删除清单和清理前快照位于 `/private/tmp/shader-cleanup-20261028/`; 是本地回退材料, 不复制回生产代码. 已保存 runs 和用户报告未删除. 原构建目录先移出应用, 防止 setuptools 把残留旧模块重新打进 wheel.

## 对外迁移

`run_analysis` / `run_analysis_task`、生成 API 与当前 CLI 核心签名保持. 旧回放命令、实验调用和旧实现模块导入已经退出支持. `AnalysisOptions` 中旧共享额度、手工上下文、分页和历史整合字段随实现删除, YAML/CLI 不再接受它们（包括原默认值）; 具体字段见[配置表](../../README.md#配置与迁移). 当前输出预算由 `infrastructure/llm/options.py` 单点解析, 仍保留阶段 CLI、通用 CLI、阶段 YAML、通用 YAML 的优先级.
