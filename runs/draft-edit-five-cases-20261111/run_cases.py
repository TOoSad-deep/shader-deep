from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import json,subprocess,os,hashlib
ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
APP=REPO/"apps/shader_deep"
e=json.loads((ROOT/"experiment.json").read_text())
for name,digest in e["runtime_source_sha256"].items():assert hashlib.sha256((REPO/name).read_bytes()).hexdigest()==digest

def run(case):
 name=case["case"]
 with (ROOT/(name+"-console.json")).open("w") as out,(ROOT/(name+".log")).open("w") as err:
  result=subprocess.run(["uv","run","--no-sync","--env-file",".env","python",str(ROOT/"one_case.py"),name],cwd=APP,env=dict(os.environ,UV_CACHE_DIR="/private/tmp/shader-deep-uv-cache"),stdout=out,stderr=err,check=False)
 print(name,"process",result.returncode,(ROOT/(name+"-console.json")).read_text().strip(),flush=True)
 return {"case":name,"process_exit":result.returncode}
with ThreadPoolExecutor(max_workers=3) as pool:
 rows=[]
 for future in as_completed([pool.submit(run,c) for c in e["cases"]]):
  rows.append(future.result());(ROOT/"batch-processes.json").write_text(json.dumps(rows,indent=2))
