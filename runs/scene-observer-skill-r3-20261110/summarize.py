"""正式回读报告并汇总程序指标, 不把状态转换成质量分数."""
from pathlib import Path
import json
import statistics
import sys
from shader_deep.infrastructure.storage.observation_package import read_observation_package

root=Path(sys.argv[1]).resolve()
experiment=json.loads((root/"experiment.json").read_text())
attempts=json.loads((root/"attempts.json").read_text())
rows=[]
for attempt in attempts:
    directory=Path(attempt["run_dir"])
    metrics=json.loads((directory/"manifest.json").read_text())
    row={**attempt,"metrics":{k:metrics.get(k) for k in ("model_id","model_calls","validation_errors","tool_refusals","resource_sha256","skill_bundle_sha256","resource_read_results","elapsed_seconds","token_usage","reason")}}
    if attempt.get("report_dir"):
        manifest,observations=read_observation_package(Path(attempt["report_dir"]))
        case=next(c for c in experiment["cases"] if c["case"]==attempt["case"])
        assert manifest.reference.sha256==case["image_sha256"]
        assert metrics["resource_sha256"]==experiment["resource_sha256"]
        row.update(counts={"E":len(observations.elements),"F":len(observations.features),"R":len(observations.relations)},gaps=[q.model_dump(mode="json") for q in manifest.gaps],formal_reader="passed")
    rows.append(row)
valid=[r for r in rows if r.get("formal_reader")=="passed"]
summary={"attempts":len(rows),"valid_packages":len(valid),"statuses":{s:sum(r["status"]==s for r in rows) for s in ("completed","partial","failed")},"logical_calls_all_attempts":sum(r["metrics"]["model_calls"] for r in rows),"sdk_reported_tokens_all_attempts":sum((r["metrics"]["token_usage"] or {}).get("total_tokens",0) for r in rows),"usage_unknown_attempts":sum(r["metrics"]["token_usage"] is None for r in rows),"median_seconds_valid_packages":statistics.median(r["metrics"]["elapsed_seconds"] for r in valid) if valid else None,"read_success_valid_cases":sum(any(x["status"]=="success" for x in r["metrics"]["resource_read_results"]) for r in valid),"rows":rows,"quality_note":"程序状态与资源读取均不证明语义质量; 人工审阅单独记录"}
(root/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
print(json.dumps({k:v for k,v in summary.items() if k!="rows"},ensure_ascii=False,indent=2))
