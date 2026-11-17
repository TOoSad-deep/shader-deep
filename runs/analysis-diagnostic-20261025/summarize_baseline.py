"""只读取本轮制品, 分开统计任务完成、提交拒绝和传输失败."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize_sample(name: str) -> dict:
    folder = ROOT / name
    metadata = json.loads((folder / "sample.json").read_text())
    result = {"sample": name, "metadata": metadata, "runs": []}
    for directory in sorted(folder.glob("run-*")):
        commit = json.loads((directory / "commit.json").read_text())
        events = read_lines(directory / "events.jsonl")
        tools = read_lines(directory / "tools.jsonl")
        usage = [event for event in events if event.get("event") == "model_usage"]
        run = {"directory": str(directory), "sealed": commit["sealed"], "tasks": [],
               "usage": {key: sum(event.get(key, 0) for event in usage) for key in ("input_tokens", "output_tokens", "total_tokens")}}
        if (directory / "run.json").exists():
            saved = json.loads((directory / "run.json").read_text())
            run["run_metadata"] = {key: saved.get(key) for key in ("stop_reason", "selected_version", "gaps", "open_questions")}
        for task_id, task in commit["tasks"].items():
            selected = [event for event in events if event.get("task_id") == task_id]
            submissions = [item for item in tools if item.get("task_id") == task_id and item.get("tool") == "submit_exploration"]
            rejections = [item for item in selected if item.get("event") == "tool_rejected"]
            row = {"task": task_id, "status": task["status"], "model_calls": task.get("model_calls", 0),
                   "attempts": len(task.get("attempts", [])), "final_error": task.get("error"),
                   "first_submission_success": submissions[0]["outcome"] == "success" if submissions else None,
                   "submission_count": len(submissions),
                   "rejection_categories": dict(Counter(item.get("category", "unknown") for item in rejections)),
                   "rejections": rejections,
                   "request_failures": [item for item in selected if item.get("event") == "request_failed"],
                   "network_seconds": round(sum(item.get("elapsed_seconds", 0) for item in selected if item.get("event") in ("request_completed", "request_failed")), 3)}
            accepted = next((item for item in submissions if item.get("outcome") == "success"), None)
            if accepted:
                arguments = accepted["arguments"]
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                report = arguments["report"]
                row["accepted_on_call"] = accepted["model_call"]
                row["report_counts"] = {key: len(report.get(key, [])) for key in ("features", "relations", "mechanisms", "sketches", "open_questions", "gaps")}
            run["tasks"].append(row)
        result["runs"].append(run)
    return result


if __name__ == "__main__":
    samples = [summarize_sample(name) for name in ("simple", "medium", "complex") if (ROOT / name / "sample.json").exists()]
    (ROOT / "live-summary.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n")
    for sample in samples:
        print(sample["sample"], "elapsed", sample["metadata"].get("elapsed_seconds"))
        for run in sample["runs"]:
            print("sealed", run["sealed"], "usage", run["usage"])
            for task in run["tasks"]:
                print(task["task"], task["status"], "calls", task["model_calls"], "rejections", task["rejection_categories"], "first_ok", task["first_submission_success"])
