"""四个独立真实探索实例, 固定输入与配置, 保留失败且不自动重派."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

for flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[flag] = "false"

from shader_deep.domain.five_libraries import ExplorationSubmission, object_index
from shader_deep.domain.five_libraries.validation import exploration_library
from shader_deep.experiments.exploration_skill.comparison import ComparisonSample, run_job
from shader_deep.experiments.exploration_skill.runner import load_skill
from shader_deep.infrastructure.llm.client import _configuration_prefix, build_model
from shader_deep.infrastructure.storage.analysis_materials import collect_materials, package_materials
from shader_deep.infrastructure.storage.report_evidence import EvidenceAsset, evidence_bytes
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.configuration import load_analysis_options

ROOT = Path(__file__).resolve().parent
APP = ROOT.parents[1] / "apps/shader_deep"
COUNT = 4
LIMIT_SECONDS = 600
LOCK = threading.Lock()
ACTIVE: dict[int, subprocess.Popen] = {}


def write(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def safe_error(error: BaseException) -> str:
    message = f"{type(error).__name__}: {error}"
    for key, value in os.environ.items():
        if len(value) >= 8 and any(word in key for word in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")):
            message = message.replace(value, "[REDACTED]")
    return message


def configuration() -> tuple[object, object, dict]:
    prefix = _configuration_prefix()
    host = urlsplit(os.environ.get(prefix + "_BASE_URL", "")).hostname
    if prefix != "OPENROUTER" or host != "openrouter.ai":
        raise ValueError("本次固定使用已核对的 OpenRouter 配置")
    model = build_model()
    options = replace(load_analysis_options(), max_worker_calls=24)
    metadata = {"model": model.model_name, "provider_group": prefix, "endpoint_host": host,
                "extra_body": model.extra_body, "options": asdict(options), "tracing": False}
    return model, options, metadata


def prepare() -> None:
    if (ROOT / "plan.json").exists():
        raise FileExistsError("已有实测计划, 拒绝覆盖")
    _, _, metadata = configuration()
    sample = ComparisonSample.model_validate_json((ROOT / "sample.json").read_text())
    if hashlib.sha256(Path(sample.image).read_bytes()).hexdigest() != sample.sha256:
        raise ValueError("固定原图哈希不一致")
    (ROOT / "skill-snapshot.md").write_text(load_skill())
    sources = {str(path.relative_to(APP)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (APP / "src/shader_deep").rglob("*.py")}
    sources["resources/analysis.yaml"] = hashlib.sha256((APP / "src/shader_deep/resources/analysis.yaml").read_bytes()).hexdigest()
    write(ROOT / "source-snapshot.json", sources)
    write(ROOT / "plan.json", {"status": "prepared", "count": COUNT, "parallel_processes": 2,
          "mode": "staged", "observation_tools": True, "compact_context": True,
          "limit_seconds_per_worker": LIMIT_SECONDS, "automatic_redispatch": False,
          "scope": "four fresh independent workers, no main agent or integration", **metadata,
          "sample": sample.model_dump(mode="json"),
          "skill_sha256": hashlib.sha256(load_skill().encode()).hexdigest()})
    print(json.dumps({"status": "prepared", "model": metadata["model"], "workers": COUNT, "directory": str(ROOT)}, ensure_ascii=False), flush=True)


def preserve_output(directory: Path, sample: ComparisonSample, result: dict) -> None:
    with TaskStore(directory) as store:
        task = store.task("worker")
        if task["status"] in {"pending", "running"}:
            store.cancel("worker", result.get("error", "独立试验已结束"))
            task = store.task("worker")
        result.update(status=task["status"], logical_calls=task["model_calls"], repairs=task["repairs"])
        package = None
        if task["status"] == "succeeded":
            report = ExplorationSubmission.model_validate(store.read_result("worker"))
            write(directory / "accepted-report.json", report.model_dump(mode="json"))
            result["accepted_report"] = "accepted-report.json"
            library = exploration_library(report, sample.target)
            materials = collect_materials(store, {"worker": {key: key for key in object_index(library)}})
            evidence, assets = package_materials(materials, sample.sha256)
            declaration = EvidenceAsset(path="evidence.json", sha256=hashlib.sha256(evidence_bytes(evidence)).hexdigest()) if evidence else None
            manifest = ReportManifest(reference=ReferenceAsset(sha256=sample.sha256), target_element_id=sample.target.id,
                status="partial" if report.gaps else "completed", open_questions=report.open_questions, gaps=report.gaps,
                context_notes=report.context_notes, evidence=declaration)
            try:
                write_report_package(directory / "report", library, manifest, Path(sample.image).read_bytes(), evidence=evidence, evidence_assets=assets)
                package = "report"
                result.update(report_status=manifest.status, report_dir=package)
            except (OSError, ValueError) as error:
                result["export_error"] = safe_error(error)
        else:
            names = [name for name in store.snapshot()["versions"] if name.startswith("draft:worker:")]
            if names:
                latest = max(names, key=lambda name: int(name.rsplit(":", 1)[1]))
                payload = store.read_version(latest)
                write(directory / "last-draft.json", payload)
                result.update(last_draft="last-draft.json", last_stage=payload["stage"], revision=payload["revision"])
        store.seal(package_path=package)


def worker(number: int) -> None:
    if not 1 <= number <= COUNT:
        raise ValueError("实例编号越界")
    directory = ROOT / f"worker-{number:02d}"
    if directory.exists():
        raise FileExistsError("实例目录已存在, 不覆盖或重跑")
    model, options, metadata = configuration()
    plan = json.loads((ROOT / "plan.json").read_text())
    if metadata["model"] != plan["model"] or metadata["options"] != plan["options"]:
        # JSON 把元组转换为列表, 比较确定性 JSON 而不是 Python 容器类型.
        if json.dumps(metadata, sort_keys=True, default=str) != json.dumps({key: plan[key] for key in metadata}, sort_keys=True):
            raise ValueError("实例配置偏离冻结计划")
    sample = ComparisonSample.model_validate_json((ROOT / "sample.json").read_text())
    job = {"sample": sample.name, "pair": "context", "condition": "compact_history", "repeat": number}
    started = time.monotonic()
    try:
        result = run_job(sample, job, model, options, directory)
    except (Exception, KeyboardInterrupt) as error:
        result = {**job, "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed", "error": safe_error(error)}
    if directory.exists():
        try:
            preserve_output(directory, sample, result)
        except (OSError, ValueError, KeyError) as error:
            result["export_error"] = safe_error(error)
    else:
        directory.mkdir()
    result.update(trial=number, wall_seconds=round(time.monotonic() - started, 3), **metadata)
    write(directory / "trial.json", result)
    print(json.dumps({key: result.get(key) for key in ("trial", "status", "logical_calls", "repairs", "wall_seconds", "counts", "error", "export_error")}, ensure_ascii=False), flush=True)


def supervised(number: int) -> dict:
    logs = ROOT / "logs"
    print(f"START worker-{number:02d}", flush=True)
    with (logs / f"worker-{number:02d}.stdout.log").open("w") as output, (logs / f"worker-{number:02d}.stderr.log").open("w") as error:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(number)], cwd=APP,
            stdout=output, stderr=error, start_new_session=True)
        with LOCK:
            ACTIVE[number] = process
        timeout = False
        try:
            code = process.wait(timeout=LIMIT_SECONDS)
        except subprocess.TimeoutExpired:
            timeout = True
            os.killpg(process.pid, signal.SIGINT)
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
        finally:
            with LOCK:
                ACTIVE.pop(number, None)
    directory = ROOT / f"worker-{number:02d}"
    metadata = {"trial": number, "exit_code": code, "observation_timeout": timeout}
    write(logs / f"worker-{number:02d}.process.json", metadata)
    if (directory / "trial.json").exists():
        result = json.loads((directory / "trial.json").read_text())
    else:
        result = {"trial": number, "status": "process_failed", "error": "No finalized trial output; inspect process log"}
    result.update(process=metadata)
    print(f"END worker-{number:02d} status={result['status']} timeout={timeout}", flush=True)
    return result


def execute() -> None:
    plan = json.loads((ROOT / "plan.json").read_text())
    if plan["status"] != "prepared" or any((ROOT / f"worker-{number:02d}").exists() for number in range(1, COUNT + 1)):
        raise ValueError("仅执行尚未启动的四实例计划")
    (ROOT / "logs").mkdir(exist_ok=False)
    plan["status"] = "running"
    write(ROOT / "plan.json", plan)
    results = []
    pool = ThreadPoolExecutor(max_workers=2)
    futures = [pool.submit(supervised, number) for number in range(1, COUNT + 1)]
    try:
        for future in as_completed(futures):
            results.append(future.result())
            write(ROOT / "summary.json", sorted(results, key=lambda item: item["trial"]))
        plan["status"] = "completed" if all(item["status"] == "succeeded" for item in results) else "completed_with_failures"
    except KeyboardInterrupt:
        plan["status"] = "interrupted"
        for future in futures:
            future.cancel()
        with LOCK:
            for process in ACTIVE.values():
                os.killpg(process.pid, signal.SIGINT)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        plan["finished_workers"] = len(results)
        write(ROOT / "plan.json", plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--worker", type=int)
    args = parser.parse_args()
    if args.prepare:
        prepare()
    elif args.execute:
        execute()
    else:
        worker(args.worker)
