"""相同齿轮样本的三组独立 worker 对照, 各重复两次, 第二轮反序."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dataclasses import asdict, replace
from urllib.parse import urlsplit

for flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[flag] = "false"

from shader_deep.domain.five_libraries import Element
from shader_deep.experiments.exploration_skill.runner import load_skill, run_exploration_variant
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.llm.messages import png_data_url
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows.configuration import load_analysis_options

ROOT = Path(__file__).resolve().parent
APP = ROOT.parent.parent / "apps/shader_deep"
IMAGE = APP / "test_pic/2d-physics-balls.png"
TARGET = Element(id="E1", name="最左上方绿色齿轮状圆片", region="原图左侧彩色堆叠区域中最左上方的绿色圆片, 包含中心孔和外轮廓", feature_ids=())
REQUEST = "请分析参考图中的一个指定元素: 彩色圆片堆中最左上方的绿色齿轮状圆片, 包含中心孔和外轮廓。以过程式 Shader 复现为目标, 独立分析可见特征、关系、候选实现机制和草图方案, 明确不确定项与缺口。本轮只分析该目标。"
DIRECTION = "围绕目标的可见特征和相互关系, 提出可用于过程式 Shader 的实现机制和草图。"
ORDER = ("baseline", "skill", "staged", "staged", "skill", "baseline")


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def run_one(number: int) -> None:
    mode = ORDER[number - 1]
    directory = ROOT / f"{number:02d}-{mode}"
    options = replace(load_analysis_options(), max_worker_calls=24)
    image = png_data_url(IMAGE)
    if urlsplit(os.environ.get("OPENROUTER_BASE_URL", "")).hostname != "openrouter.ai":
        raise SystemExit("只允许已授权的 OpenRouter 目的地")
    model = build_model()
    execution = AnalysisExecution()
    meta = {"mode": mode, "trial": number, "model": model.model_name, "request": REQUEST, "direction": DIRECTION,
            "target": TARGET.model_dump(mode="json"), "reference_sha256": hashlib.sha256(IMAGE.read_bytes()).hexdigest(),
            "options": asdict(options), "skill_sha256": hashlib.sha256(load_skill().encode()).hexdigest(), "langsmith_tracing": False,
            "automatic_redispatch": False, "scope": "one independent worker; no main or integration"}
    started = time.monotonic()
    with TaskStore(directory) as store:
        store.register("exploration-1", meta["reference_sha256"], {"user_request": REQUEST, "target": TARGET.model_dump(mode="json"), "direction": DIRECTION, "mode": mode})
        attempt = store.start_attempt("exploration-1")
        write(directory / "trial.json", meta)
        try:
            run_exploration_variant(store, attempt, model, options, execution, REQUEST, image, TARGET, DIRECTION, mode=mode)
        except (Exception, KeyboardInterrupt) as error:
            meta["error"] = f"{type(error).__name__}: {error}"
            if store.is_active(attempt):
                store.fail_attempt(attempt, meta["error"], permanent=True)
        task = store.task(attempt.task_id)
        meta.update(status=task["status"], model_calls=task["model_calls"], repairs=task["repairs"], elapsed_seconds=round(time.monotonic()-started, 3))
        if task["status"] == "succeeded":
            result = store.read_result(attempt.task_id)
            write(directory / "accepted-report.json", result)
            meta["counts"] = {key: len(result.get(key, [])) for key in ("features", "relations", "mechanisms", "sketches", "open_questions", "gaps")}
        store.seal()
        write(directory / "trial.json", meta)
    print(json.dumps({key:meta.get(key) for key in ("trial","mode","status","model_calls","repairs","elapsed_seconds","counts","error")},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_one(int(sys.argv[1]))
    else:
        skill = load_skill()
        (ROOT / "skill-snapshot.md").write_text(skill)
        write(ROOT / "source-snapshot.json", {str(path.relative_to(APP)):hashlib.sha256(path.read_bytes()).hexdigest() for path in (APP/"src/shader_deep").rglob("*.py")})
        for number, mode in enumerate(ORDER, 1):
            directory = ROOT / f"{number:02d}-{mode}"
            if directory.exists():
                raise SystemExit(f"已有试验目录, 拒绝覆盖: {directory}")
            directory.mkdir()
            print(f"START {number} {mode}", flush=True)
            with (directory/"stdout.json").open("w") as output, (directory/"stderr.log").open("w") as error:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(number)], cwd=APP, stdout=output, stderr=error, start_new_session=True)
                timed_out=False
                try:
                    code=process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    timed_out=True
                    os.killpg(process.pid,signal.SIGINT)
                    try: code=process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL);code=process.wait()
            write(directory/"process.json", {"exit_code":code,"observation_timeout":timed_out})
            print(f"END {number} {mode} exit={code} timeout={timed_out}",flush=True)
