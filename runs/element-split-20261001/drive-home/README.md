# drive-home

![参考图](reference.png)

状态: `dispatch_prepared_not_started`; 实测时长 147.06 秒; 实际主规划调用 1 次; 子角色调用 0 次.

## 接受的规划

```json
{
  "elements": [
    {
      "id": "e",
      "name": "b",
      "region": "g",
      "feature_ids": []
    }
  ],
  "target_element_id": "e",
  "directions": [
    "单",
    "颗",
    "水"
  ]
}
```

语义检查: **存在占位内容, 不构成可用的元素拆分结果**: e.name=b, e.region=g.

## 每次模型提交及校验反馈

### 主规划调用 1: success

```json
{
  "model_call": 1,
  "run_id": "drive-home",
  "task_id": "planning",
  "attempt_id": "f8a9282b700e42ac922bc0f306e2986f",
  "tool": "submit_target_plan",
  "call_id": "call_fcc1ed32c35549939aad06c0",
  "outcome": "success",
  "scope": "planning",
  "arguments": {
    "plan": {
      "directions": [
        "单",
        "颗",
        "水"
      ],
      "elements": [
        {
          "feature_ids": [],
          "id": "e",
          "name": "b",
          "region": "g"
        }
      ],
      "target_element_id": "e"
    }
  },
  "response": "{\"status\": \"accepted\", \"result_path\": \"results/57473ae92ff0948e996eab4629bb64269ba685bccb80fb8fe9859c89eeee9a6b.json\"}"
}
```

## 原始记录

[完整日志](analysis.log) · [终端日志](console.log) · [模型返回](model-output.jsonl) · [工具审计](tools.jsonl) · [请求事件](events.jsonl) · [持久任务状态](commit.json)

[待派发完整指令](dispatch.json) · [边界快照](boundary-state.json)
