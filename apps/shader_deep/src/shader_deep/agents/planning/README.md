# 全图规划角色

[native.py](native.py) 的 build_planning_agent 装配一个原生 Deep Agents 角色。输入固定为原图、完整 E/F/R、用户要求、观察问题与 gaps，以及实际生成约束。

工具为 save_planning_draft、read_file、edit_file、finish_planning 和 stop_planning。模型首次保存 M/S 草稿，随后只编辑 /draft/planning.json。可见工具和执行白名单由角色及[共享草稿运行辅助](../draft_runtime.py)同时约束。

[scene-planner/SKILL.md](../../resources/skills/scene-planner/SKILL.md) 直接进入专用角色 system prompt。复杂方法拆合、备选迁移和条件缺口通过条件参考读取。只读观察与约束也预置在 /inputs/ 虚拟文件中。

每次实际主请求注入固定材料与原图，持久消息和摘要由 SDK 管理。草稿回读版本的完整正文实际进入后续请求后，工作流才允许接受。状态与发布见[规划工作流](../../workflows/planning/README.md)。

运行记录保存实际模型标识、显式推理等级、资源哈希、调用计数和读取结果。推理配置未指定时记录空值。
