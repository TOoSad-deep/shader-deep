# 独立视觉评审角色

[native.py](native.py) 的 `build_review_agent` 装配一个原生 Deep Agents 角色。共同规则与选定的 feature、element 或 scene 模板直接进入固定指令。

[context.py](context.py) 在每次实际模型请求注入固定标准、参考图、候选图与可选局部视图。每张图片都有用途标签，实际捕获字节成为多模态图像块。生成者自检和源码不进入这份上下文。

配置区域统计或覆盖检查时，消息追加程序证据。模型使用统计证据解释差异及影响，程序覆盖未评项由工作流合并到正式提交。可见问题与只有采集不足的必查项通过 `check` 和未评项区分。

调用方声明的 `source.candidate_id`、`source.run_id` 和涵盖来源声明的完整输入摘要保存在运行材料和正式报告中。模型只收到评审所需的标准和图像材料，同一组图像和标准更换来源标签后，模型看到的内容相同。

V1 使用 `submit_review` 和 `stop_review` 提交六维报告或停止。V2 按[逐项规则](../../resources/skills/visual-review/items.md)运行。模型可在同一轮依次用 `record_review_check` 登记多项，再以 `finish_review` 提交或以 `stop_review` 停止。[middleware.py](middleware.py) 按本轮协议限制工具可见性与执行，并按声明顺序处理同批调用。终态与逻辑调用预算在下一次主模型请求前检查。

V2 普通登记保留已经回答的项目。确需改判时，`revise_review_check` 接受当前版本、同一检查项的新答案、改判理由和可核查的证据说明。工具和实际模型上下文的 `answer_revisions` 提供版本，从 1 开始。过时版本返回冲突。首次重评前，运行中的 `results/check_answers.json` 是答案列表。发生重评后，一次文件替换同时保存 `check_answers` 和 `check_revisions`。接受快照保存同一份审计，正式报告保存在 `metadata.check_revisions`。每条审计记录前后答案、修订版本、模型轮次、理由和依据。

OpenRouter 请求绑定本轮工具时启用严格 schema. 工具对象及嵌套报告对象关闭额外属性, 全部声明属性列入必填, 集合可以提交空数组.

提交由领域合同检查六维状态、问题、未评项及绑定范围。视觉结论由程序派生。业务会话、接受快照和正式发布见[评审工作流](../../workflows/review/README.md)，指标含义见[评审准则](../../../../docs/visual-review-rubric.md)。

[原生角色测试](../../../../tests/unit_tests/agents/test_native_review.py)使用无网络模型经过真实 SDK 图检查图片、工具、修正、终态和预算。工程命令见[开发指南](../../../../docs/development.md)。
