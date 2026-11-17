"""执行固定四项 ABBA 清单, 不重跑失败任务."""
import json
import subprocess
import sys
from pathlib import Path

root=Path(__file__).resolve().parent
jobs=[('deep_agent',1),('create_agent',2),('create_agent',3),('deep_agent',4)]
progress={'status':'running','completed':[],'active':None}
for condition,number in jobs:
    progress['active']={'condition':condition,'number':number}
    (root/'progress.json').write_text(json.dumps(progress,indent=2))
    result=subprocess.run([sys.executable,str(root/'evaluate.py'),'trial','--condition',condition,'--number',str(number)],timeout=270,check=False)
    if result.returncode:
        progress['status']='infrastructure_failure'
        break
    progress['completed'].append({'condition':condition,'number':number})
else:
    progress['status']='finished'
progress['active']=None
(root/'progress.json').write_text(json.dumps(progress,indent=2))
