"""固定样本顺序运行当前公开分析入口, 保留原始输出与观察超时."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

# 本轮用户明确要求关闭外部追踪, 设置由子进程继承, 不改写 .env.
for tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[tracing_flag] = "false"

from shader_deep.infrastructure.llm.client import _configuration_prefix
from shader_deep.workflows.configuration import load_analysis_options

ROOT = Path(__file__).resolve().parent
APP = ROOT.parent.parent / "apps" / "shader_deep"
SAMPLES = (
    ("simple", "shiny-rectancle.png", "图中央的完整霓虹圆角矩形边框, 包含紧邻边框的光晕"),
    ("medium", "2d-physics-balls.png", "彩色圆片堆中最左上方的绿色齿轮状圆片, 包含中心孔和外轮廓"),
    ("complex", "supah-frosted-glass.png", "画面右上方的前景磨砂玻璃圆片, 包含它与后方蓝色圆片重叠的区域"),
)


def save(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def run_sample(name: str, filename: str, target: str) -> bool:
    destination = ROOT / name
    destination.mkdir(exist_ok=False)
    image = APP / "test_pic" / filename
    prompt = f"请分析参考图中的一个指定元素: {target}。以过程式 Shader 复现为目标, 独立分析可见特征、关系、候选实现机制和草图方案, 明确不确定项与缺口。本轮只分析该目标。"
    command = [sys.executable, "-m", "shader_deep.cli.analysis", str(image), prompt, "--output-dir", str(destination), "--log-level", "WARNING"]
    metadata = {"sample": name, "image": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "prompt": prompt,
                "started_utc": datetime.now(timezone.utc).isoformat(), "observation_timeout_seconds": 900, "command": command}
    save(destination / "sample.json", metadata)
    print(f"START {name}", flush=True)
    started = time.monotonic()
    with (destination / "stdout.json").open("w") as output, (destination / "stderr.log").open("w") as errors:
        process = subprocess.Popen(command, cwd=APP, stdout=output, stderr=errors, start_new_session=True)
        timed_out = False
        try:
            code = process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGINT)
            try:
                code = process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
    metadata.update(exit_code=code, observation_timed_out=timed_out, elapsed_seconds=round(time.monotonic()-started, 2), ended_utc=datetime.now(timezone.utc).isoformat())
    save(destination / "sample.json", metadata)
    print(f"END {name} exit={code} elapsed={metadata['elapsed_seconds']} timeout={timed_out}", flush=True)
    events = [json.loads(line) for path in destination.glob("run-*/events.jsonl") for line in path.read_text().splitlines() if line.strip()]
    if not any(event.get("event") == "request_completed" for event in events):
        print("STOP: no completed network request; inspect transport before remaining samples", flush=True)
        return False
    return True


if __name__ == "__main__":
    prefix = _configuration_prefix()
    if prefix != "OPENROUTER" or urlsplit(os.environ.get("OPENROUTER_BASE_URL", "")).hostname != "openrouter.ai":
        raise SystemExit("配置目的地不是已授权的 OpenRouter, 停止发送")
    save(ROOT / "authorization.json", {"destination": "openrouter.ai", "model": os.getenv("OPENROUTER_MODEL"),
         "samples": [sample[1] for sample in SAMPLES], "langsmith_tracing": False,
         "user_authorized": "允许发送到 OpenRouter，关闭 LangSmith"})
    if not (ROOT / "baseline.json").exists():
        save(ROOT / "baseline.json", {"model": os.getenv(prefix + "_MODEL"), "provider_group": prefix,
             "reasoning_effort": os.getenv(prefix + "_REASONING_EFFORT", "provider_default"),
             "options": asdict(load_analysis_options()), "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP, text=True).strip(),
             "source_sha256": {str(path.relative_to(APP)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((APP / "src" / "shader_deep").rglob("*.py"))},
             "note": "Unchanged application code; one trial per image; default options except output location; no skill treatment."})
    for sample in SAMPLES:
        if (ROOT / sample[0]).exists():
            print(f"SKIP existing {sample[0]}", flush=True)
            continue
        if not run_sample(*sample):
            break
