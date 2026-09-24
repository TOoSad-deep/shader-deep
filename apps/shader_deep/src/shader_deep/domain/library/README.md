# 四库业务与发布边界

本模块维护统一元素、组成草图、外观特征和组织关系, 保留形成机制候选及其来源. 它校验结构、引用和修改权限, 不判断哪个机制在视觉上正确, 不调用模型或直接操作文件.

全局位置见[应用架构](../../../../docs/architecture.md), 设计取舍见 [ADR 0001](../../../../docs/decisions/0001-agent-oriented-layout.md).

## 阅读顺序

| 文件 | 重点 |
| --- | --- |
| [models.py](models.py)、[validation.py](validation.py) | 原始探索报告、最终四库和引用/范围规则 |
| [store.py](store.py) | `add_report`、`apply_integration` 与统一版本发布 |
| [documents.py](documents.py)、[merging.py](merging.py) | 身份命名空间、引用变换和待提交副本中的合并 |
| [queries.py](queries.py)、[reading.py](reading.py) | 目录、完整正文、分片及实际呈现登记 |
| [materials.py](materials.py) | 比较目标、材料版本与未完成工作 |
| [persistence.py](persistence.py) | 持久化端必须满足的最小契约 |

[queue.py](queue.py) 保留旧材料包调度策略; 当前默认整合使用 `ComparisonManager`, 不自动启用旧队列.

## 数据关系

`VisualOutline` 固定元素与实例组身份. 独立探索产生 `ExplorationReport`, 入库时为局部身份绑定报告命名空间, 形成 `PossibilityLibrary`. 元素和三个库构成最终四个顶层集合; 候选附属于特征或关系, 草图通过引用选择可能组成.

合并外观特征不表示其机制等价. 合并拥有者保留不同候选, 候选归并必须满足自己的归属与前提约束. 别名用于解析历史身份, 来源记录保留原作者与报告, 不用改写原报告来伪造一致结论.

## 一次整合如何提交

```text
实际请求呈现材料 → 登记材料 ID 与版本
  → apply_integration 检查提交身份、材料新鲜度、呈现和修改范围
  → 在副本中执行全部操作并校验最终库
  → 持久化完整版本、回执及处理进度
  → 发布内存版本并返回回执
```

读取目录、登记记录和将完整正文放入模型请求是不同事件. `present_materials` 只能由实际请求准备路径调用; 材料版本变化后, 曾经读过旧版本不能授权新修改.

`LibraryStore` 拥有库内容、版本、来源、别名和接受回执. 相同提交身份及相同内容重放返回原回执; 同身份不同内容被拒绝. 调用方不能直接修改内部字典绕过校验和发布.

`apply_integration` 和原报告提交使用库锁. 工作流负责同轮写入调度, 不能推断任意公开辅助方法都可由多个线程无序调用.

## 存储失败与适配

业务库接受 `LibraryPersistence`; [LibraryFiles / FileLibraryStore](../../infrastructure/storage/library.py) 提供文件实现和历史消息入口适配. 框架消息先转成调用身份、正文与失败标志, 业务层只核对业务回执.

持久化版本失败时, 已接受的内存版本与回执不推进. 原报告文件可能已先保存, 不能因此声称整次入库已成功; 重试仍按原报告内容和接受状态处理. 单个版本快照的写入不等于多个文件的事务.

当前工作流的 `completed` 由交付组件计算. 库更新成功不等于整轮完成, 更不代表视觉验收.

## 修改后如何验证

在 `apps/shader_deep/` 执行:

```sh
uv run --no-sync python -W error -m unittest discover -s tests/unit_tests/domain -q
make check
```

优先阅读[事务与幂等用例](../../../../tests/unit_tests/domain/test_integration_library.py)、[发布失败用例](../../../../tests/unit_tests/domain/test_library_persistence.py)和[比较状态用例](../../../../tests/unit_tests/domain/test_comparison_state.py). 修改材料选择时, 同时检查当前整合的请求呈现和跨版本行为.
