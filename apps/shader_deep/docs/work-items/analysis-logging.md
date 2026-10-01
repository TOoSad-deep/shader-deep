# 分析关键步骤日志

## 范围与行为

用户要求在后台打印元素拆分、子 Agent 分析结果等关键步骤. 本轮仅增加默认五库分析的可观测性, 不改变业务 Schema、修复额度、角色权限、公开函数签名和结果判定.

- `infrastructure/analysis_logging.py` 统一追加每轮 `analysis.log`, 并通过 `shader_deep.analysis` 标准 logger 输出终端诊断. 日志写入与业务提交分离, 文件日志故障不使任务失败.
- CLI 默认 `--log-level INFO`, stderr 显示运行及任务开始、元素与范围、探索方向、F/R/M/S 摘要、校验拒绝、网络恢复、用量、版本发布和最终状态. stdout 仍只有原 JSON 索引, 退出码保持原语义.
- `--log-level DEBUG` 在终端展示完整业务提交及回执、模型正文、请求材料统计和五库版本. 文件始终保存所有级别, INFO 长正文摘要显式标记截断.
- UTC 时间和 run/task/attempt/call 身份区分并行输出; call 为 `-` 表示该条不是具体模型调用节点. 标准日志 handler 与文件写锁保证一条多行记录完整写出.
- 模型响应、提交接受/拒绝、版本发布分别标明. 接受提交不证明内容质量; 日志不进入模型上下文, 不承担恢复状态.
- 不输出完整模型请求、图片 Base64 或推理字段, 客户端凭据不传入日志. 新文本输出对已定义敏感字段和常见密钥/鉴权形式脱敏. 原 `events.jsonl` 与 `tools.jsonl` 审计格式保留.
- Python API 保存文件日志, 终端沿用宿主日志配置; CLI 使用临时 handler, 结束和异常退出后恢复原配置.

用户运行方法见[README 的实时日志说明](../../README.md#实时运行日志).

## 2026-10-01 验证

代码状态: 本地未提交的日志实现, 在既有 OpenRouter 适配改动上进行; 保留其他未提交与未跟踪文件.

在应用目录执行 `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check`:

- 仓库检查: 0 findings.
- 无网络 unittest: 475 passed, 警告按错误处理.
- Ruff、格式和 ty: 通过.
- 完整输出: `/private/tmp/shader-deep-logging-check.log`.

新增/扩展覆盖: stdout 可解析且不混入日志, INFO 显示实际拆分与探索摘要, ERROR 过滤只影响终端, 文件仍包含详细正文, 并行角色身份可区分, 失败 worker 保留 partial 与其他报告, 敏感字段脱敏, 文件写入故障仍显示进度并恢复宿主 logger.

无网络 CLI 演示保存在 `runs/logging-smoke-20261001/`: 固定模型夹具让一个探索方向失败, 其余正常运行至 V1 与 partial 报告. 最新演示目录 `run-nnt8n149`, 人工检查 `console.log` 及 `analysis.log` 的拆分、子任务、失败、发布和结束节点; `index.json` 为合法 JSON. 示例中的视觉内容来自固定测试夹具, 不是真实模型对参考图的分析结果.

未进行新的真实模型请求、浏览器渲染或视觉验收. 本轮日志未提交、推送或合并.
