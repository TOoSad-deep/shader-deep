# 模型适配

## 职责

从用户配置创建模型客户端, 编码真实图像, 解析阶段输出预算, 适配 OpenRouter 及检查完整工具响应. 角色装配和框架中间件在[分析角色](../../agents/analysis/README.md), 业务提交由工作流与领域层负责.

## 阅读入口

1. [client.py](client.py) 的 `build_model`: 整组选用模型、地址与凭据, 仅创建客户端.
2. [options.py](options.py) 的 `resolve_role_options`: 把公开运行选项解析为最小传输配置.
3. [transport.py](transport.py) 的 `configure_analysis_model` / `validate_analysis_message`: 分析客户端配置及执行工具前的原文校验.
4. [openrouter.py](openrouter.py) 处理网关 Schema、推理块与结束帧; [messages.py](messages.py) 编码图片与多模态消息.

## 输入、输出与状态

输入为环境变量、已校验的阶段选项和框架消息. 输出为客户端、请求载荷或通过严格校验的消息; 服务密钥不进入业务记录.

每次流式请求独立持有 `ReasoningBuffer` 和 `StreamTerminal`. 客户端连接可以共享, 这里不持有任务槽位、报告版本或模型会话历史. 同步配置保留调用方已有连接, 不在某个子任务结束时关闭其他角色仍使用的连接.

## 常见改动

| 需求 | 修改入口 |
| --- | --- |
| 调整服务配置选择 | `client.py`; 三组环境变量始终整组选用 |
| 调整输出预算优先级 | `options.py`; 显式来源由[工作流配置](../../workflows/configuration.py)记录 |
| 处理提供方 Schema 或推理协议差异 | `openrouter.py` 与 `AnalysisChatOpenAI` 的请求/响应适配 |
| 处理截断、歧义 JSON 或流式结束 | `validate_analysis_message`、`decode_object`、`StreamTerminal` |
| 修改重试次数或图调用上限 | 修改角色所用框架中间件, 不在这里增加任务重派循环 |

## 关键约定

- 配置优先级为 `OPENROUTER_*` → `DS_MICU_*` → `MICU_*`. 首个任一项非空的组必须完整, 不跨组借用地址或凭据; `.env` 由启动命令加载.
- 输出预算依次采用阶段 CLI、通用 CLI、显式阶段值、通用值. YAML/CLI 来源必须随原值核对, 不能让过期标签改变覆盖顺序.
- 分析客户端关闭 SDK 内部重试, 由角色的 `ModelRetryMiddleware` 承担有限请求重试. 生成客户端的重试策略独立, 不在此处推断全局费用或调用总数.
- 保存原始工具参数, 拒绝截断、重复键、缺少正常结束标记或身份歧义. 流式解析器补出的 JSON 不能直接成为可执行参数.
- OpenRouter 外发 Schema 的适配不放宽本地领域校验; 推理块保留原消息归属和稳定元数据.
- data URL 提供真实图片字节, 路径或 JSON 中的图像说明不能代替多模态内容块.

## 验证入口

[配置](../../../../tests/unit_tests/infrastructure/test_config.py)、[阶段预算](../../../../tests/unit_tests/workflows/test_phase_configuration.py)、[原始响应](../../../../tests/unit_tests/infrastructure/test_native_transport.py)、[OpenRouter](../../../../tests/unit_tests/infrastructure/test_openrouter_transport.py). 普通检查使用无网络夹具; 真实服务验证需单独限定范围. 命令与文档维护见[开发指南](../../../../docs/development.md)和[同步约定](../../../../AGENTS.md#文档同步).
