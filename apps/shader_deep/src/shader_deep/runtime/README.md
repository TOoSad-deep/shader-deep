# 角色执行机制

本模块提供当前分析角色与历史兼容会话使用的执行循环、预算、历史整理和提交修复. 角色决定提示词、材料及工具, 工作流决定调用顺序和整体交付; runtime 不导入具体 Agent 或工作流.

生成角色仍使用自己的[生成循环](../agents/generation/agent.py), 本模块不是全应用唯一的循环实现. 全局职责见[应用架构](../../../docs/architecture.md).

## 阅读入口

| 文件 | 负责什么 |
| --- | --- |
| [runner.py](runner.py) | `AnalysisLoop`: 组装请求、限制工具、计数与执行 |
| [execution.py](execution.py) | 每个执行实例的状态、尝试及反馈记录 |
| [budgets.py](budgets.py)、[options.py](options.py) | 完整请求预算与最小配置协议 |
| [history.py](history.py) | 在请求副本中整理已消费回执, 保留调用配对 |
| [submissions/handler.py](submissions/handler.py) | 提交、完整草稿、校验反馈与局部修复 |
| [submissions/parsing.py](submissions/parsing.py) | 严格参数解析和有限格式恢复 |
| [events.py](events.py)、[usage.py](usage.py) | 事件契约、无进展识别与实际用量记录 |

## 一次调用的顺序

```text
检查结束状态和剩余调用额度
  → 整理请求历史, 构造角色材料
  → 检查完整请求预算, 登记实际呈现
  → 计一次逻辑模型调用
  → 获取完整响应, 必要时仅重试网络请求
  → 校验工具权限与参数, 按响应顺序执行工具
  → 保存反馈, 判断是否继续
```

同一响应中的工具按声明顺序执行. `max_concurrency=1` 约束单个角色图, 避免停止与提交争抢会话锁; 工作流中独立探索任务的外层并发不受影响. 工具列表限制和执行白名单都必须保留.

网络恢复在[传输适配器](../infrastructure/llm/transport.py)内, 不重放已完成的业务工具. 流式响应未完整结束时不能提前执行工具, 格式恢复不能猜补缺失业务参数.

## 状态与预算

一个角色持有自己的 `AnalysisExecution`、历史、提交处理器和无进展状态. 新建一次底层图不应重置角色已消耗的额度或修复记录. 整轮额度由调用方绑定, 初稿与整合共享额度的具体规则见[整合说明](../agents/integration/README.md).

逻辑模型调用和网络尝试分别计数. 请求预算包含系统指令、工具 schema、材料、历史、图像估算及输出预留; 字符估算不能当作服务端 token 用量. 未收到实际用量时保留不可用状态.

局部修复绑定草稿身份、版本和稳定作用域. 补丁只允许受限 JSON 操作, 修复后重新走原提交校验. 业务拒绝不冒充语法错误; 普通错误、无进展和额度耗尽保留明确反馈, 不靠换循环清空历史额度.

`history.py` 只压缩已消费的支持类型回执和已被完整草稿替代的参数. 未呈现正文和活动草稿不能被假定已读, 也不能破坏工具调用与响应配对.

## 资源与完成边界

文件写入由存储组件负责, 事件输出由[追踪适配器](../infrastructure/tracing.py)负责. 循环结束仅表示当前角色结束; 库是否合法、任务是否部分完成、结果是否通过视觉验收属于不同的判断层.

## 修改后如何验证

在 `apps/shader_deep/` 执行:

```sh
uv run --no-sync python -W error -m unittest discover -s tests/unit_tests/runtime -q
make check
```

恢复、传输和历史协议的完整用例仍有一部分在[兼容测试](../../../tests/unit_tests/compatibility/). 改变工具执行或结束判断时, 必须保留[同一响应先停止再提交的回归](../../../tests/unit_tests/compatibility/test_exploration_flow.py), 不能只断言配置里出现了并发参数.
