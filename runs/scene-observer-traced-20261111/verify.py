from pathlib import Path
from types import SimpleNamespace
import json,time,sys
from langsmith import Client
ROOT=Path(__file__).resolve().parent
name=sys.argv[1]
index=json.loads((ROOT/(name+"-index.json")).read_text())
root=SimpleNamespace(id=index["trace_id"])
client=Client(); project_record=client.read_project(project_name=index["project"])
verification={}
for attempt in range(8):
    try:
        remote=client.read_run(root.id)
        children=list(client.list_runs(project_id=project_record.id,trace_id=root.id,select=["id","name","run_type","error","end_time","inputs","outputs"],limit=100))
        llms=[r for r in children if r.run_type=="llm"]
        tool_runs=[r for r in children if r.run_type=="tool"]
        verification={"trace_id":str(root.id),"url":client.get_run_url(run=remote,project_id=project_record.id),"root_finished":remote.end_time is not None,"runs":len(children),"llm_runs":len(llms),"tool_runs":len(tool_runs),"image_in_llm_input":any("data:image/" in json.dumps(r.inputs) for r in llms),"llm_outputs_present":any(bool(r.outputs) for r in llms),"error_runs":sum(bool(r.error) for r in children)}
        if verification["root_finished"] and llms and (tool_runs or index["status"]=="failed"):
            break
    except Exception as error:
        verification={"trace_id":str(root.id),"verification_error_type":type(error).__name__}
    time.sleep(2)
(ROOT/(name+"-trace.json")).write_text(json.dumps(verification,ensure_ascii=False,indent=2)+"\n")
print(json.dumps({"case":name,"status":index["status"],**verification},ensure_ascii=False),flush=True)
