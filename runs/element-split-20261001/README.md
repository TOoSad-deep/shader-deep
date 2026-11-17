# 元素拆分与派发前截停测试

日期: 2026-10-01 (Asia/Shanghai).

使用当前工作区代码、默认 analysis.yaml 和既有 .env 模型配置, 按顺序测试 5 张仓库 PNG. 不修改生产工作流、提示词或公开 API. `provenance.json` 保存测试起始 Git 状态与关键源码哈希.

统一用户要求见各目录 `input.json`. 图片名称只用于目录标识, 不加入模型提示词.

## 截停边界

复用真实 `_planning` 和 `_explore`: 主 agent 调用 `submit_target_plan`, 完成本地 schema 与业务校验, 持久化规划; `_explore` 按真实逻辑登记探索任务. 探针替换 `_wait_workers`, 在其创建 ThreadPoolExecutor 前保存派发参数并截停. 探索、整合、恢复角色入口另加禁止执行保护; 规划失败不启动恢复角色. 规划内部保留原有网络重试、提交修复及自动重派.

`dispatch.json` 为实际登记的待派发任务参数, 不是主 agent 的额外派发工具调用. 子 agent 尚未执行, 因而不生成五库包、不发布 V0/V1、不把截停标成完整分析成功.

## 每次运行的记录

- `reference.png`: 固定原始图片字节.
- `input.json`: 图片来源、哈希、用户要求、模型 ID 与执行参数, 无凭据.
- `model-output.jsonl`: 模型正文、解析后的工具调用、响应元数据与 token 用量; 不保存推理块、图片 Base64 或 API 密钥.
- `tools.jsonl`: 工具提交参数、校验反馈与回执.
- `events.jsonl`: 请求、重试、用量和角色事件.
- `analysis.log` 和 `console.log`: 应用 DEBUG 日志与终端诊断.
- `plan.json`: 通过校验的元素列表、唯一目标与探索方向.
- `dispatch.json`: 派发前待启动任务的完整参数.
- `commit.json`, `results/`, `boundary-state.json`, `final-state.json`: 原生持久记录与截停快照.
- `output.json`: 单次运行状态、时长及子角色执行次数.

`batch.log` 保存批处理 stdout/stderr; `summary.json` 随每张图片结束写入.

## 离线探针验证

应用目录运行 `PYTHONPATH=. UV_CACHE_DIR=/private/tmp/shader-deep-uv-cache uv run --no-sync python ../../runs/element-split-20261001/run_probe.py --smoke`. 固定模型夹具只用于验证截停: 1 个规划请求、3 个 pending 探索任务、0 个 worker 请求. 正式五张图片均使用真实服务.

初次受限环境网络解析失败保存在 `sandbox-network-failure/`, 未获得合法规划或派发指令. 此次预检失败触发了恢复任务登记, 保护入口未执行恢复模型; 随后加强为登记之前禁用恢复分支. 正式运行使用允许联网的环境. 不把该预检计入 5 张正式结果.

## 正式结果

[查看五张正式结果与逐次记录](results.md). 4 张到达派发前截停, 1 张规划失败; 总计 12 个待派发探索任务, 实际子角色请求 0 次. 极光和夜间驾车的接受规划存在占位内容, 不可用于后续分析. 图像真实内容为彩色齿轮单元和 Windows 标志, 文件名不是模型提示材料.
