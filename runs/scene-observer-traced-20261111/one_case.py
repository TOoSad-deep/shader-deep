from pathlib import Path
import contextlib,io,json,os,sys,time,hashlib
os.environ["LANGSMITH_TRACING"]="true"
os.environ["LANGCHAIN_TRACING_V2"]="true"
os.environ["LANGSMITH_TRACING_SAMPLING_RATE"]="1"
from langsmith import Client,trace
from langsmith.utils import get_tracer_project
from langchain_core.tracers.langchain import wait_for_all_tracers
from shader_deep.cli.analysis import main
ROOT=Path(__file__).resolve().parent
E=json.loads((ROOT/"experiment.json").read_text())
SKILL=ROOT.parents[1]/"apps/shader_deep/src/shader_deep/resources/skills/scene-observer"
assert all(hashlib.sha256((SKILL/n).read_bytes()).hexdigest()==h for n,h in E["resource_sha256"].items())
name=sys.argv[1]
case=next(x for x in E["cases"] if x["case"]==name)
assert hashlib.sha256(Path(case["image"]).read_bytes()).hexdigest()==case["image_sha256"]
client=Client(); project=get_tracer_project()
assert project==E["langsmith_project"]
project_record=client.read_project(project_name=project)
buffer=io.StringIO()
sys.argv=["shader-deep-analyze",case["image"],E["prompt"],"--output-dir",str(ROOT/name),"--max-model-calls","8","--max-request-retries","0","--request-timeout-seconds","45","--max-output-tokens","163840","--log-level","WARNING"]
with trace("observer-retest/"+name,project_name=project,client=client,inputs={"case":name},tags=[E["batch_tag"],name],metadata={"skill_bundle_sha256":E["skill_bundle_sha256"]}) as root:
    with contextlib.redirect_stdout(buffer):
        code=main()
    index=json.loads(buffer.getvalue())
    root.add_outputs({"status":index["status"],"run_dir":index["run_dir"]})
index.update(trace_id=str(root.id),project=project,exit_code=code)
(ROOT/(name+"-index.json")).write_text(json.dumps(index,ensure_ascii=False,indent=2)+"\n")
wait_for_all_tracers();client.flush(timeout=30)
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
