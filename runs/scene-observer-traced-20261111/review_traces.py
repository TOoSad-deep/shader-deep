from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
from langsmith import Client
ROOT=Path(__file__).resolve().parent
summary=json.loads((ROOT/'summary.json').read_text())
def compact(r):
 return {'relations_present':'relations' in r,'relations_count':len(r.get('relations',[])),'gaps_count':len(r.get('gaps',[]))}
def calls(v):
 found=[]
 if isinstance(v,dict):
  if v.get('name')=='submit_scene_observations' and isinstance(v.get('args'),dict):found.append(compact(v['args'].get('report',{})))
  for child in v.values():found.extend(calls(child))
 elif isinstance(v,list):
  for child in v:found.extend(calls(child))
 return found
def check(row):
 c=Client(); runs=list(c.list_runs(project_name='shader-deep',trace_id=row['trace']['trace_id'],select=['id','name','run_type','start_time','inputs','outputs','error'],limit=100)); events=[]
 for run in sorted(runs,key=lambda x:x.start_time):
  if run.run_type=='llm':events.append({'type':'llm','submitted_calls':calls(run.outputs)})
  if run.run_type=='tool':
   data={'type':'tool','name':run.name,'error':bool(run.error)}
   if isinstance(run.inputs,dict) and isinstance(run.inputs.get('report'),dict):data.update(compact(run.inputs['report']))
   if run.name=='read_file':data['file_path']=run.inputs.get('file_path')
   data['result_excerpt']=str(run.outputs)[:450];events.append(data)
 return {'case':row['case'],'events':events}
with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(check,summary['rows']))
(ROOT/'trace-review.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(rows,ensure_ascii=False,indent=2))
