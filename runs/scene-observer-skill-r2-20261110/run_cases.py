"""按冻结配置回归十图, 原始输出各自保存, 不自动重试."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import os
import subprocess
import time

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
APP = REPO / "apps/shader_deep"
EXPERIMENT = json.loads((ROOT / "experiment.json").read_text())
SKILL = APP / "src/shader_deep/resources/skills/scene-observer"
for name, digest in EXPERIMENT["resource_sha256"].items():
    assert hashlib.sha256((SKILL / name).read_bytes()).hexdigest() == digest


def run_case(case):
    name = case["case"]
    image = Path(case["image"])
    assert hashlib.sha256(image.read_bytes()).hexdigest() == case["image_sha256"]
    arguments = ["uv", "run", "--no-sync", "--env-file", ".env", "shader-deep-analyze", str(image), EXPERIMENT["prompt"],
        "--output-dir", str(ROOT / name), "--max-model-calls", "8", "--max-request-retries", "0",
        "--request-timeout-seconds", "45", "--max-output-tokens", "163840", "--log-level", "WARNING"]
    environment = dict(os.environ, LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false", UV_CACHE_DIR="/private/tmp/shader-deep-uv-cache")
    started = time.perf_counter()
    with (ROOT / f"{name}-index.json").open("w") as output, (ROOT / f"{name}.log").open("w") as error:
        result = subprocess.run(arguments, cwd=APP, env=environment, stdout=output, stderr=error, check=False)
    summary = {"case": name, "returncode": result.returncode, "process_seconds": time.perf_counter() - started}
    try:
        index = json.loads((ROOT / f"{name}-index.json").read_text())
        summary.update({key:index.get(key) for key in ("status", "run_dir", "report_dir")})
    except (OSError, json.JSONDecodeError):
        summary["status"] = "no_index"
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_case, case) for case in EXPERIMENT["cases"]]
        for future in as_completed(futures):
            rows.append(future.result())
            (ROOT / "attempts.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
