"""汇总原始记录, 不把字段校验通过当作视觉语义成功."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
rows = json.loads((root / 'summary.json').read_text())
report = ['# 五张图片元素拆分测试结果', '', '日期: 2026-10-01. 模型与执行条件见 [测试说明](README.md) 和各目录 input.json.', '', '| 图片 | 规划模型调用 | 元素数 | 派发任务数 | 子角色调用 | 结果 |', '| --- | ---: | ---: | ---: | ---: | --- |']
for row in rows:
    directory = root / row['image']
    state = json.loads((directory / 'final-state.json').read_text())
    planning = state['tasks']['planning']
    dispatch = json.loads((directory / 'dispatch.json').read_text()) if (directory / 'dispatch.json').exists() else None
    model_output = [json.loads(line) for line in (directory / 'model-output.jsonl').read_text().splitlines()] if (directory / 'model-output.jsonl').exists() else []
    tools = [json.loads(line) for line in (directory / 'tools.jsonl').read_text().splitlines()] if (directory / 'tools.jsonl').exists() else []
    assert all(not task['attempts'] and task.get('model_calls', 0) == 0 for key, task in state['tasks'].items() if key != 'planning')
    assert all(item['task_id'] == 'planning' for item in model_output)
    success = row['status'] == 'dispatch_prepared_not_started'
    if success:
        assert len(dispatch['tasks']) == 3
        assert all(task['status'] == 'pending' for task in dispatch['tasks'].values())
    detail = [f"# {row['image']}", '', '![参考图](reference.png)', '', f"状态: `{row['status']}`; 实测时长 {row['elapsed_seconds']} 秒; 实际主规划调用 {planning.get('model_calls', 0)} 次; 子角色调用 0 次.", '', '## 接受的规划', '']
    placeholders = []
    if (directory / 'plan.json').exists():
        plan = json.loads((directory / 'plan.json').read_text())
        detail += ['```json', json.dumps(plan, ensure_ascii=False, indent=2), '```', '']
        placeholders = [f"{e['id']}.{key}={e[key]}" for e in plan['elements'] for key in ('name', 'region') if len(e[key]) == 1 and e[key].isascii() and e[key].isalpha()]
        if placeholders:
            detail += ['语义检查: **存在占位内容, 不构成可用的元素拆分结果**: ' + ', '.join(placeholders) + '.', '']
        else:
            detail += ['字段与任务状态验证通过; 元素范围和探索方向的语义质量尚未由用户验收.', '']
    else:
        detail += ['未形成合法规划. 最终错误: ' + str(planning.get('error')), '']
    detail += ['## 每次模型提交及校验反馈', '']
    for tool in tools:
        detail += [f"### 主规划调用 {tool['model_call']}: {tool['outcome']}", '', '```json', json.dumps(tool, ensure_ascii=False, indent=2), '```', '']
    detail += ['## 原始记录', '', '[完整日志](analysis.log) · [终端日志](console.log) · [模型返回](model-output.jsonl) · [工具审计](tools.jsonl) · [请求事件](events.jsonl) · [持久任务状态](commit.json)', '']
    if success:
        detail += ['[待派发完整指令](dispatch.json) · [边界快照](boundary-state.json)', '']
    (directory / 'README.md').write_text('\n'.join(detail), encoding='utf-8')
    status = '到达派发前截停' if success else '规划失败'
    if placeholders:
        status += '; 占位内容不可用'
    report.append(f"| [{row['image']}]({row['image']}/README.md) | {planning.get('model_calls', 0)} | {row.get('elements', '—')} | {len(dispatch['tasks']) if dispatch else 0} | 0 | {status} |")
report += ['', '验证: 所有正式记录中的模型请求均归属 planning; 所有非 planning 任务的 attempts 为空、model_calls 为 0. 成功截停目录的 3 个探索任务均为 pending. 未创建五库交付包或探索结果.', '', '未修改生产代码、提示词、模型配置或既有工作区改动. 原始被拒绝输出按实保留, 未人工补全为有效派发结果.', '']
(root / 'results.md').write_text('\n'.join(report), encoding='utf-8')
print(f'Report written for {len(rows)} cases; all worker calls are 0.')
