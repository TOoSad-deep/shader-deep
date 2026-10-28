# 分析架构与 Agent 工作流设计

当前选择是 Deep Agents 原生编排与最小业务适配: SDK 执行主/子角色, 应用维护固定输入、准入、五库校验和交付. 选择理由见 [ADR 0001](decisions/0001-agent-oriented-layout.md), 跨模块关系见[架构总览](architecture.md).

## 按问题进入当前说明

| 问题 | 主要说明位置 |
| --- | --- |
| 主/探索/整合怎样装配, 工具和上下文怎样分配 | [分析角色](../src/shader_deep/agents/analysis/README.md) |
| 谁固定输入, 谁判断成功, 怎样汇集与交付 | [分析工作流](../src/shader_deep/workflows/analysis/README.md) |
| 五库字段、引用及草图含义怎样保持 | [五库领域](../src/shader_deep/domain/five_libraries/README.md)及[详细数据设计](analysis-five-libraries.md) |
| 模型服务、预算或传输完整性怎样适配 | [模型适配](../src/shader_deep/infrastructure/llm/README.md) |
| 报告及证据怎样发布、读取与传给生成 | [文件存储](../src/shader_deep/infrastructure/storage/README.md) |

## 维护范围

角色实现和业务规则分别在所属模块维护, 本页不复制第二份执行合同. 改动职责或关键约束时更新模块 README; 跨模块关系变化再更新架构总览, 重要且难以理解的设计取舍记录于 ADR.

查历史设计与实测经过时再读[五库实施记录](work-items/five-library-implementation.md)、[原生切换](work-items/deep-agents-native-plan-20261028.md)或[旧执行清理](work-items/analysis-legacy-cleanup-20261028.md). 历史方案与当时测试记录不代表当前接口或当前验证状态.
