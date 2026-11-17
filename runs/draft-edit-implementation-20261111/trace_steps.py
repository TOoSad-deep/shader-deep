from pathlib import Path
import json
from langsmith import Client
root=Path(__file__).resolve().parent
client=Client();rows=[]
for name in ['pink','pink-retry','frosted']:
 index=json.loads((root/(name+'-index.json')).read_text())
 runs=list(client.list_runs(project_name='shader-deep',trace_id=index['trace_id'],run_type='tool',select=['name','inputs','outputs','start_time','error'],limit=100))
 steps=[]
 for run in sorted(runs,key=lambda x:x.start_time):
  step={'name':run.name,'error':bool(run.error)}
  output=(run.outputs or {}).get('output',{})
  if isinstance(output,dict):
   step['status']=output.get('status')
   try:
    content=json.loads(output.get('content',''))
    if isinstance(content,dict):step.update({k:content[k] for k in ['created','saved','accepted','code','revision','accepted_revision'] if k in content})
   except (ValueError,TypeError):pass
  steps.append(step)
 rows.append({'case':name,'status':index['status'],'steps':steps})
(root/'trace-steps.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(rows,ensure_ascii=False,indent=2))
