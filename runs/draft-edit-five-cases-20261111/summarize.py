from pathlib import Path
from collections import Counter
import json
from shader_deep.infrastructure.storage.observation_package import read_observation_package
root=Path(__file__).resolve().parent
e=json.loads((root/'experiment.json').read_text())
rows=[]
for case in e['cases']:
 name=case['case'];p=root/(name+'-index.json')
 if not p.exists():
  rows.append({'case':name,'status':'no_index'});continue
 index=json.loads(p.read_text());trace=json.loads((root/(name+'-trace.json')).read_text()) if (root/(name+'-trace.json')).exists() else {}
 row={'case':name,'status':index['status'],'run_dir':index['run_dir'],'report_dir':index['report_dir'],'trace':trace}
 metrics=json.loads((Path(index['run_dir'])/'manifest.json').read_text())
 row['reason']=metrics.get('reason');row['draft_path']=str(Path(index['run_dir'])/metrics['draft_path']) if metrics.get('draft_path') else None;row['draft_revision']=metrics.get('draft_revision');row['seen_revision']=metrics.get('seen_revision');row['accepted_revision']=metrics.get('accepted_revision');row['model']=metrics.get('model_id');row['elapsed_seconds']=metrics.get('elapsed_seconds');row['model_calls']=metrics.get('model_calls')
 row['trace_verified']=bool(trace.get('root_finished') and trace.get('llm_runs') and trace.get('image_in_llm_input') and (trace.get('tool_runs') or index['status']=='failed'))
 if index['report_dir']:
  assert metrics['draft_revision']==metrics['seen_revision']==metrics['accepted_revision']
  manifest,report=read_observation_package(Path(index['report_dir']))
  assert manifest.reference.sha256==case['image_sha256']
  assert metrics['skill_bundle_sha256']==e['skill_bundle_sha256']
  row.update(counts={'E':len(report.elements),'F':len(report.features),'R':len(report.relations)},gaps=[x.model_dump(mode='json') for x in manifest.gaps],formal_reader='passed')
 rows.append(row)
summary={'cases':len(rows),'statuses':dict(Counter(x['status'] for x in rows)),'trace_verified':sum(x.get('trace_verified',False) for x in rows),'project':e['langsmith_project'],'tag':e['batch_tag'],'skill_bundle_sha256':e['skill_bundle_sha256'],'rows':rows}
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
lines=['# 五图草稿工具回归与 LangSmith 轨迹','',f"项目: {e['langsmith_project']}; 筛选标签: `{e['batch_tag']}`.",'','使用当前草稿保存、回读、局部编辑与完成工具. 本轮冻结 skill 与运行源码, 使用既有模型及8轮预算, 并发3.',f"状态: {summary['statuses']}; 已远程核验轨迹: {summary['trace_verified']}/{len(rows)}.",'','报告状态不代表语义质量验收; 本次没有逐图重新评定质量分数.','','| Case | 状态 | E/F/R | LangSmith | 本地报告 |','| --- | --- | --- | --- | --- |']
for r in rows:
 counts='/'.join(str(n) for n in r.get('counts',{}).values()) or '—'
 url=r.get('trace',{}).get('url');tr=f'[轨迹]({url})' if url else '未确认'
 report=f"[报告]({r['report_dir']}/README.md)" if r.get('report_dir') else '无报告'
 lines.append(f"| {r['case']} | {r['status']} | {counts} | {tr} | {report} |")
lines+=['','## 模型声明的缺口','']
for r in rows:
 for gap in r.get('gaps',[]):lines.append(f"- {r['case']}: {gap['description']}")
(root/'README.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({k:v for k,v in summary.items() if k!='rows'},ensure_ascii=False))
