# 分析工作流

## 职责

固定本轮输入, 创建业务记录, 调用一次原生主图, 根据实际接受的产物核查状态并交付报告. 角色定义和模型工具循环在[分析角色](../../agents/analysis/README.md); 字段、引用与合并规则在[五库领域](../../domain/five_libraries/README.md).

## 阅读入口

1. [entry.py](entry.py): `run_analysis` / `run_analysis_task` 固定原图、创建输出目录并返回公开 `AnalysisOutcome`.
2. [pipeline.py](pipeline.py): `execute_five_analysis` 单次调用图, 正常返回、异常或中断后都核查业务产物.
3. [session.py](session.py): `AnalysisSession` 的计划、槽位、提交、版本和最终核查; 返回合同见 [contracts.py](contracts.py).

## 输入、输出与状态

公开入口接受 PNG 与要求, 或已登记的根分析任务和 `AnalysisOptions`. 输出包括本轮状态、结果索引和可选 `report_dir`; 无合法基线时不发布空报告包.

| 责任方 | 持有内容 |
| --- | --- |
| `AnalysisSession` | 固定输入、元素、冻结计划、槽位状态、接受的报告/材料、V0/V1 和缺口 |
| SDK 图 | 消息历史、todos、scratch 和摘要; 工作流不保存第二份模型会话 |
| 文件 | `manifest.json` 业务索引、`results/` 合法产物、`result.json` 终态、`run.json` 公开投影和 `report/` 交付包 |

槽位按 `pending → running → succeeded/failed` 推进; 主图提前结束时尚未完成的槽位也会记失败. 成功提交后不会重开槽位, 普通模型文本不能把失败改成成功.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 固定要求、原图或公开返回 | `entry.py` |
| 方向数量、准入前置条件、终态 | `session.py` 的 `freeze_plan`、`admit_task`、`finalize` |
| 合法来源汇集、证据随版本变化 | `_collect_baseline`、`_materials`、`submit_integration`; 具体引用规则仍由领域层负责 |
| 运行参数及 YAML/CLI 优先级 | [options.py](../options.py)、[configuration.py](../configuration.py); 阶段值由[模型适配](../../infrastructure/llm/README.md)解析 |
| 报告文件格式或读取 | 转到[文件存储](../../infrastructure/storage/README.md) |

## 关键约定

- 原图、目标和本批两个或三个方向固定后保持不变. RLock 将槽位检查与占用、提交与接受置于同一临界区, 模型调用不持有该锁.
- 接受前校验, 接受时复制报告与嵌套材料. 当前子任务只能提交自己的产物, 不能借父图描述或兄弟报告扩展输入.
- 全部探索成功或失败后才准入整合. V0 汇集合法来源并记录缺失方向; V1 只接受保义合并, 草图不会被重新设计.
- 最终优先 V1, 否则交付可用 V0 并保留缺口. 没有合法基线为 failed; 图返回后不补问、不重派, 再次执行创建新运行.
- 报告包发布成功才登记公开结果. 快照是文件记录, 不承诺多文件事务或跨进程模型会话恢复; 中断会在尝试交付已有合法产物后继续传播.

## 验证入口

[公开入口与失败交付](../../../../tests/unit_tests/workflows/test_five_public_api.py)、[配置加载](../../../../tests/unit_tests/workflows/test_analysis_config.py)、[阶段覆盖](../../../../tests/unit_tests/workflows/test_phase_configuration.py). 命令见[开发检查](../../../../docs/development.md), 维护见[同步约定](../../../../AGENTS.md#文档同步).
