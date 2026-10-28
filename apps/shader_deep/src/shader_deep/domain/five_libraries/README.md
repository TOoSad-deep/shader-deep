# 五库领域规则

## 职责

定义五库对象, 校验来源内/包内引用, 汇集独立报告并应用保留含义的合并建议. 本模块不读写文件、不调用模型, 也不决定任务是否重试或结束. 数据含义与完整字段设计以[五库数据设计](../../../../docs/analysis-five-libraries.md)为主要说明; 当前可执行合同以源码为准.

## 阅读入口

1. [models.py](models.py): `ExplorationSubmission`、`FiveLibraries` 和 `MergeProposal`.
2. [validation.py](validation.py) 与 [references.py](references.py): 提交、候选选择和文本引用校验.
3. [operations.py](operations.py): `collect_explorations` 汇集来源, `apply_merges` 应用整合建议.

## 输入、输出与状态

| 对象 | 含义 |
| --- | --- |
| E / Element | 固定分析对象 |
| F / Feature、R / Relation | 必须复现的观察及其联系 |
| M / Mechanism | 候选实现方法, 不等于已选择方案 |
| S / Sketch | 为 F/R 选择机制并说明组织方式; 局部备选覆盖指定选择 |

输入是固定目标与独立探索提交, 或完整五库与合并建议. 输出为新的五库快照、问题/缺口及来源映射. 本模块不持有运行状态, V0/V1 名称、保存和版本选择由[工作流](../../workflows/analysis/README.md)管理.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 增加字段或调整含义 | `models.py`、相关校验及数据设计; 同步核对工具 schema 和文件包消费者 |
| 规则涉及引用、草图候选或缺口 | `validation.py`、`references.py` |
| 改汇集后的 ID 或合并行为 | `operations.py`; 保持正文、结构引用和来源映射一致 |
| 改模型观察方式或输出表达引导 | 转到[分析角色](../../agents/analysis/README.md)的 skill |
| 改报告包装或证据文件 | 转到[文件存储](../../infrastructure/storage/README.md) |

## 关键约定

- 对象类型由所属库判定, 不凭 ID 前缀猜测. 结构字段使用完整 ID, 正文对象引用写成 `[[ID]]`; 普通变量不能被顺带重命名.
- 各探索可以使用相同本地 ID. 汇集为包内分配 ID 并同步重写引用, 保留各来源内容; 语义去重另走显式合并建议.
- 整合仅合并已有 F/R/M 并保留组内既有项. 选择目标撞键、共同机制或关系端点塌缩等会改变草图含义的情况必须拒绝.
- 候选机制不代表默认采用; 草图所选机制必须属于对应 F/R 的候选. 草图未覆盖本来源观察时须记录缺口, 不强制每个来源的草图数量.
- `context_notes` 与 `evidence_links` 是提交辅助材料. 本模块校验业务引用, 证据是否生成、进入请求和对应真实像素由角色及存储层验证.
- 引用合法与合并通过只证明这些程序合同, 不证明观察或实现机制在视觉上正确.

## 验证入口

[五库汇集与合并](../../../../tests/unit_tests/domain/test_five_libraries.py)、[探索提交合同](../../../../tests/unit_tests/domain/test_exploration_contracts.py). 按[开发指南](../../../../docs/development.md)验证, 文档更新遵循[同步约定](../../../../AGENTS.md#文档同步).
