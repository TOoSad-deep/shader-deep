# OpenRouter 五库分析适配与验证

## 范围与实现

- 用户指定 `z-ai/glm-5.3-flash` 与 `https://openrouter.ai/api/v1`, 要求适配后执行一轮完整分析.
- `client.py` 按 `OPENROUTER_*`、`DS_MICU_*`、`MICU_*` 顺序整组选用配置; 全空占位跳过, 不完整组直接报错, 不跨组补齐.
- OpenRouter 使用 Chat Completions 与 `provider.require_parameters=true`; 分析传输使用网关 `max_tokens`, 保留原始工具参数检查.
- `openrouter.py` 汇集流式 `reasoning_details`, 稳定字段不做字符串拼接, 在后续工具请求中回传完整推理块. 同步与流式路径均有真实 SDK 编解码测试.
- 相同 SSE 结束帧去重, 冲突结束原因仍拒绝. 可选 `OPENROUTER_REASONING_EFFORT` 只覆盖显式推理强度, 未设置时保留提供方默认.
- 可选 `OPENROUTER_PROVIDER_SORT` 控制选路优先级; 分析请求为函数工具声明 `strict: true`, 保留动态选择字典 Schema. 实测提供方仍会返回非法参数, 本地校验继续承担接受与拒绝的最终边界.
- 探索提示补全既有业务契约: 草图选择键是已声明 F/R ID, 值是该对象的候选机制 ID 数组; 文本引用使用 `[[ID]]`, 修复保留完整观察产物. 不改变字段或放宽校验.
- 不新增依赖, 不改变五库业务字段、Agent 工具权限或公开函数签名. 已有真实 `.env` 未修改.

协议来源: [模型目录](https://openrouter.ai/z-ai/glm-5.3-flash)、[推理块协议](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)、[提供方选路](https://openrouter.ai/docs/guides/routing/provider-selection)、[LangChain 工具与严格输出](https://openrouter.ai/blog/tutorials/langchain-chatopenrouter-setup/).

## 2026-09-30 离线验证

代码状态: 本地未提交的 OpenRouter 客户端与分析传输适配.

在 `apps/shader_deep/` 执行 `UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache make check`:

- 仓库链接与导入边界: 0 findings.
- 无网络单元测试: 471 passed, 警告按错误处理.
- Ruff、格式与 ty: 通过.
- 完整日志: `/private/tmp/shader-deep-openrouter-check.log`.

检查未运行真实模型或浏览器; 不证明模型语义质量或用户视觉验收.

## 真实验证状态

已通过无凭据的 OpenRouter 官方模型目录请求确认该 ID 存在, 支持 image、tools、max_tokens 与 reasoning; 默认推理必须启用, 默认 effort 为 max. 本地指定配置组完整, 密钥未输出.

使用 `test_pic/shiny-rectancle.png`, 分析中央蓝青色发光圆角矩形框及辉光. 前轮每请求输出预算 32768、超时 240 秒; 最终轮预算 8192、超时 120 秒. 每 worker 调用上限 8, 主角色调用上限 32. 网络超时不是整轮墙钟上限, 活跃流式请求仍可能持续较长时间.

首次真实运行命令被自动审批拒绝: 要求明确确认图片与目的地. 用户随后明确同意使用该测试图、已有凭据与指定 OpenRouter 模型, 包含真实调用费用; 后续真实请求均在该范围内.

- `runs/openrouter-glm-5.3-flash-20260930/run-n1vs_zv5`: 规划阶段重复缺少合法结束标记, 主动 SIGINT 停止, 保存 failed 投影与封存记录, 无报告包.
- 原始协议探针实际收到 HTTP 200、`[DONE]` 和两个相同 `tool_calls` 结束帧. SDK 原路径把标记拼成 `tool_callstool_calls`, 可用本地 SDK 回放精确复现. 修复后的测试接受重复相同标记, 拒绝冲突标记; 完整性检查保持不变. 脱敏探针记录位于上述运行父目录的 `protocol-probe.json`, 不含认证头、原图载荷或推理正文; 临时探针脚本已删除.
- `run-pv442kjb`: 协议修复后规划成功, 三个探索均因 schema、选择键、引用或占位 ID 错误耗尽持久修复额度, 没有合法 V0, 最终 failed. 11 次逻辑调用、12 次网络请求, 有 1 次流式连接错误恢复. 已记录用量为输入 99276、输出 90096 tokens, 其中推理 57268; 失败网络请求的未报告用量不包含在该统计中.
- `run-28oh9q47`: 补全契约示例后使用 low 推理复测, 两个视角耗尽修复额度, 最后一个视角请求长时间不结束后主动 SIGINT 停止; 最终 failed, 无 V0 或报告包. 11 次逻辑调用与网络请求, 已报告输入 82309、输出 81202 tokens; 中断请求的未报告用量不包含在该统计中.

## 最终轮结果与局限

运行目录为 `runs/openrouter-glm-5.3-flash-20260930/run-_28t737e`, 配置为 low 推理、throughput 选路、严格工具声明、8192 输出预算. 已执行规划、三视角探索、整合与文件包发布; CLI 正常返回 1, 状态 partial, 选择 V1.

- 规划成功 (3 次调用), exploration-1 成功 (1 次), exploration-2/3 各 3 次调用后修复额度耗尽, 整合成功 (2 次). 共 12 次逻辑调用与网络请求, 0 次传输错误.
- 已报告输入 35574、输出 9253 tokens. 原图落盘至最终提交记录更新时间约 96.8 秒, 属于文件时间估算的墙钟耗时.
- 五库计数: 元素 1、特征 1、关系 0、机制 0、草图 0. 3 项 gaps, 其中 2 项明确记录失败视角.
- **产物不可用于后续 Shader 生成**: 被接受的目标/范围与观察文本过短, 例如目标名为“蓝”、范围为“图”、一个缺口为“本”; 没有机制与草图. 结构校验通过和 V1 发布成功不证明分析语义质量, 不将该轮描述为成功交付完整分析方案.
- `read_report_package` 重新验证了库结构、引用与原图哈希, 并人工读取 README; 交付目录含八个文件. V1 不可变正文与完整包相等, gaps 与版本正文一致; 已封存包指针指向 report, run.json.execution 等于权威 commit.json. 所有四轮均已封存, 无遗留在途分析任务.
- 机器索引、脱敏协议探针、独立诊断日志和额外核验记录保留在运行父目录. 失败轮和模型原始工具草稿没有作为最终报告发布.

可复现命令 (在 `apps/shader_deep/` 执行, 保持既有 `.env`):

```sh
OPENROUTER_REASONING_EFFORT=low OPENROUTER_PROVIDER_SORT=throughput \
  UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache \
  uv run --no-sync --env-file .env python -m shader_deep.cli.analysis \
  test_pic/shiny-rectancle.png \
  '只分析参考图中央蓝青色发光圆角矩形框这一元素。目标范围包含细亮边缘与周围辉光，黑色内外背景作为对照上下文。请独立分析其可见特征、关系与可用于单 Pass GLSL 重建的候选机制，保留不同实现方案和不确定项，不把视觉推测当成物理事实。完成默认三视角探索、整合与五库报告交付。' \
  --output-dir runs/openrouter-glm-5.3-flash-20260930 \
  --max-output-tokens 8192 --max-worker-calls 8 --max-main-calls 32 \
  --request-timeout-seconds 120
```

适配尚未提交、推送或合并.
