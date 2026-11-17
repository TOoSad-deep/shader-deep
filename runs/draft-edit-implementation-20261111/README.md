# 草稿与局部修订实施验证

本轮验证粉圆、磨砂叠圆两张参考图, 共三次运行尝试. 模型为 z-ai/glm-5.3-flash, max_model_calls=8, LangSmith 项目 shader-deep, 标签 draft-edit-implementation-20261111.

| 尝试 | 状态 | 主模型调用 | 实际工具过程 | 证据 |
| --- | --- | --- | --- | --- |
| pink | failed | 2 | save_observation_draft | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c870-1a7e-7ef2-ab46-c81cb1a524a4?poll=true) |
| frosted | completed | 5 | save_observation_draft → read_file → edit_file → read_file → finish_analysis | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c871-53c6-79c2-9054-469b923e0fdf?poll=true) · [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-implementation-20261111/frosted/run-tot0qg3a/report/README.md) |
| pink-retry | completed | 7 | save_observation_draft → read_file → edit_file → edit_file → edit_file → read_file → finish_analysis | [轨迹](https://smith.langchain.com/o/fcb09333-f0ce-45e7-9ea2-c4750cdfbd16/projects/p/2e0945b1-fc78-4930-96bb-3085745b703a/r/01a1c871-682b-7383-a438-adf0bdb9c745?poll=true) · [报告](/Users/douwen/Documents/HUAWEl/Shader-Agent/shader-deep/runs/draft-edit-implementation-20261111/pink-retry/run-9tw9u9j4/report/README.md) |

粉圆首轮在保存草稿后的第二次模型请求发生连接错误, 草稿与失败轨迹已保留. 其独立重试成功完成三次局部编辑. 磨砂叠圆成功完成一次局部编辑. 两份正式报告均通过读取器与原图哈希校验, 接受版本与最新已见版本一致.

离线验证覆盖重复 ID/错误引用草稿、局部修复保留关系、禁止全文覆盖、陈旧版本、部分/截断回读、实际请求移除正文、同步/异步工具批次冲突以及写入/发布失败. make check 通过276项测试、Ruff、格式、ty及仓库检查. wheel已核对新增模块和四份skill资源.

本轮真实验证覆盖两张图. 报告completed表示新交付合同完成, 语义质量仍按原有观察标准审阅. 粉圆重试用了7/8轮, 后续应单独测量调用预算对更多修订的覆盖.
