# 五张图片元素拆分测试结果

日期: 2026-10-01. 模型与执行条件见 [测试说明](README.md) 和各目录 input.json.

| 图片 | 规划模型调用 | 元素数 | 派发任务数 | 子角色调用 | 结果 |
| --- | ---: | ---: | ---: | ---: | --- |
| [hypnotic-ripples](hypnotic-ripples/README.md) | 3 | — | 0 | 0 | 规划失败 |
| [auroras](auroras/README.md) | 1 | 1 | 3 | 0 | 到达派发前截停; 占位内容不可用 |
| [drive-home](drive-home/README.md) | 1 | 1 | 3 | 0 | 到达派发前截停; 占位内容不可用 |
| [2d-physics-balls](2d-physics-balls/README.md) | 1 | 5 | 3 | 0 | 到达派发前截停 |
| [windows-95](windows-95/README.md) | 1 | 3 | 3 | 0 | 到达派发前截停 |

验证: 所有正式记录中的模型请求均归属 planning; 所有非 planning 任务的 attempts 为空、model_calls 为 0. 成功截停目录的 3 个探索任务均为 pending. 未创建五库交付包或探索结果.

未修改生产代码、提示词、模型配置或既有工作区改动. 原始被拒绝输出按实保留, 未人工补全为有效派发结果.
