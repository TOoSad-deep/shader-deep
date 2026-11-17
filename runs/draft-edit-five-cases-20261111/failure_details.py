from pathlib import Path
import json
from langsmith import Client
root=Path(__file__).resolve().parent
client=Client(); rows=[]
def errors(value,found):
 if isinstance(value,dict):
  if value.get('status')=='error' and isinstance(value.get('content'),str):found.add(value['content'][:1000])
  for v in value.values():errors(v,found)
 elif isinstance(value,list):
  for v in value:errors(v,found)
for name in ['buffer-bloom','auroras']:
 d=json.loads((root/(name+'-index.json')).read_text()); found=set()
 runs=list(client.list_runs(project_name='shader-deep',trace_id=d['trace_id'],run_type='llm',select=['inputs'],limit=100))
 for run in runs:errors(run.inputs,found)
 tools=list(client.list_runs(project_name='shader-deep',trace_id=d['trace_id'],run_type='tool',select=['name','error'],limit=100))
 rows.append({'case':name,'feedback':sorted(found),'tool_errors':[{'name':r.name,'error':r.error[:1400]} for r in tools if r.error]})
(root/'failure-details.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(rows,ensure_ascii=False,indent=2))
