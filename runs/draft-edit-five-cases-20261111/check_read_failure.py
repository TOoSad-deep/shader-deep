from pathlib import Path
import json,re
from langsmith import Client
root=Path(__file__).resolve().parent;c=Client();rows=[]
for name in ['buffer-bloom','auroras']:
 d=json.loads((root/(name+'-index.json')).read_text());folder=Path(d['run_dir']);events=[]
 runs=list(c.list_runs(project_name='shader-deep',trace_id=d['trace_id'],run_type='tool',select=['name','inputs','outputs','start_time'],limit=100))
 for run in sorted(runs,key=lambda x:x.start_time):
  if run.name!='read_file':continue
  out=(run.outputs or {}).get('output',{});text=out.get('content','') if isinstance(out,dict) else ''
  h=re.search(r'@@ lines (\d+)-(\d+) of (\d+) @@',text);rev=re.search(r'Draft revision: ([0-9a-f]+)',text)
  row={'args':run.inputs,'header':h.group(0) if h else None,'revision':rev.group(1) if rev else None,'preview':text[:180],'tail':text[-180:]}
  if h and rev:
   snap=folder/'draft/history'/f'{rev.group(1)}.json'
   if not snap.exists():snap=folder/'draft/observations.json'
   lines=snap.read_text().splitlines();start,end,total=map(int,h.groups());body='\n'.join(lines[start-1:end]);row.update(source_lines=len(lines),body_matches=body in text,requested_coverage=[start,end,total])
  events.append(row)
 rows.append({'case':name,'reads':events})
(root/'read-failure-diagnostic.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print(json.dumps(rows,ensure_ascii=False,indent=2))
