from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
from langsmith import Client
root=Path(__file__).resolve().parent
rows=json.loads((root/'summary.json').read_text())['rows']
def check(row):
 c=Client()
 runs=list(c.list_runs(project_name='shader-deep',trace_id=row['trace']['trace_id'],run_type='llm',select=['extra'],limit=1))
 params=runs[0].extra.get('invocation_params',{}) if runs else {}
 body=params.get('extra_body') or {}
 reasoning=body.get('reasoning') or {}
 return {'case':row['case'],'invocation_params_available':bool(params),'reasoning':{k:reasoning[k] for k in ['effort','enabled','exclude','max_tokens'] if k in reasoning},'reasoning_effort':params.get('reasoning_effort')}
with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(check,rows))
(root/'reasoning-config-verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(results,ensure_ascii=False,indent=2))
