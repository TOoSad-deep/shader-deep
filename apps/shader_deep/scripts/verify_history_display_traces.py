"""只读核验历史展示实验的远端追踪元数据, 不读取图片载荷或调用模型."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from langsmith import Client

if TYPE_CHECKING:
    from langsmith.schemas import Run

# 不选择 inputs、outputs、events、extra, 避免下载完整多模态历史.
SELECT = ["id", "name", "run_type", "trace_id", "parent_run_id", "start_time", "end_time", "error", "session_id"]


def load_json(path: Path) -> dict:
    """读取已保存的本地运行回执, 不改写原始材料."""
    return json.loads(path.read_text())


def callback_counts(directory: Path) -> dict[str, int | None]:
    """单独统计回调和发送前预算阻止事件, 不把它们算成 HTTP 尝试."""
    path = directory / "events.jsonl"
    if not path.is_file():
        return {"callback_model_starts": None, "callback_model_errors": None, "http_budget_blocked_events": None}
    kinds = [json.loads(line).get("kind") for line in path.read_text().splitlines() if line.strip()]
    return {
        "callback_model_starts": kinds.count("model_start"),
        "callback_model_errors": kinds.count("model_error"),
        "http_budget_blocked_events": kinds.count("http_budget_blocked"),
    }


def run_metadata(run: Run) -> dict[str, object]:
    """只输出身份、父子关系、时间和错误存在性, 不输出远端错误正文."""
    return {
        "id": str(run.id),
        "name": run.name,
        "run_type": run.run_type,
        "trace_id": str(run.trace_id),
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "start_time": run.start_time.isoformat() if run.start_time else None,
        "end_time": run.end_time.isoformat() if run.end_time else None,
        "has_error": bool(run.error),
    }


def local_counts(record: dict, directory: Path, *, judge: bool) -> dict:
    """保留真实发送数与逻辑回调数, 缺失材料保持未知."""
    requests = record.get("requests" if judge else "http_requests")
    attempts = len(requests) if isinstance(requests, list) else record.get("http_attempts")
    return {
        "local_status": record.get("status"),
        "http_attempts": attempts,
        "reported_http_attempts": record.get("http_attempts"),
        **({} if judge else callback_counts(directory)),
    }


def verify_target(client: Client, record: dict, directory: Path, *, identifier: str, project: str, judge: bool) -> dict:
    """拉取根和所有 LLM 后代的轻量元数据, 保留缺失、进行中和查询失败."""
    run_id = record.get("trace_id") if judge else record.get("trace", {}).get("run_id")
    check = {"id": identifier, "kind": "judge" if judge else "tuning", "run_id": run_id, **local_counts(record, directory, judge=judge)}
    if not run_id:
        return {**check, "verification_status": "missing_local_run_id"}
    try:
        roots = list(client.list_runs(run_ids=[run_id], select=SELECT))
        if not roots:
            return {**check, "verification_status": "missing_root", "root_found": False}
        return {**check, **remote_counts(client, roots[0], str(run_id), project, check)}
    except Exception as error:  # noqa: BLE001  # 外部核验失败仅形成缺口, 不覆盖本地实验事实或输出可能含凭据的异常正文.
        return {**check, "verification_status": "query_error", "error_type": type(error).__name__}


def remote_counts(client: Client, root: Run, run_id: str, project: str, local: dict) -> dict:
    """完整消费 SDK 的游标分页迭代器, 不设置截断为 100 的总数量上限."""
    # list_runs 不设 limit 会随 next cursor 逐页读取; judge 的 LLM 根也在此集合中.
    llms = list(client.list_runs(trace_id=run_id, run_type="llm", select=SELECT))
    pending = sum(run.end_time is None for run in llms)
    attempts, starts = local.get("http_attempts"), local.get("callback_model_starts")
    return {
        "verification_status": "pending" if root.end_time is None or pending else "terminal",
        "root_found": True,
        "root": run_metadata(root),
        "url": client.get_run_url(run=root, project_name=project),
        "llm_runs": len(llms),
        "llm_ended": len(llms) - pending,
        "llm_pending": pending,
        "llm_error_runs": sum(bool(run.error) for run in llms),
        "llm_runs_minus_http_attempts": len(llms) - attempts if isinstance(attempts, int) else None,
        "llm_runs_match_http_attempts": len(llms) == attempts if isinstance(attempts, int) else None,
        "llm_runs_match_callback_starts": len(llms) == starts if isinstance(starts, int) else None,
        "judge_single_llm_root": len(llms) == 1 and str(llms[0].id) == run_id if local["kind"] == "judge" else None,
        "llm_metadata": [run_metadata(run) for run in llms],
    }


def verify(directory: Path, client: Client) -> dict:
    """核验六个调参根及全部已登记评审根, 只生成独立核验报告."""
    suite = load_json(directory / "suite.json")
    checks = []
    for case in suite["cases"]:
        path = directory / case["id"] / "summary.json"
        if not path.is_file():
            checks.append({"id": case["id"], "kind": "tuning", "verification_status": "missing_local_summary"})
            continue
        checks.append(
            verify_target(client, load_json(path), path.parent, identifier=case["id"], project=suite.get("project", "shader-deep"), judge=False)
        )
    checks.extend(
        verify_target(client, load_json(path), path.parent, identifier=path.parent.name, project=suite.get("project", "shader-deep"), judge=True)
        for path in sorted((directory / "judges").glob("*/summary.json"))
    )
    return {
        "verified_at": datetime.now(UTC).isoformat(),
        "suite": str(directory),
        "expected_tuning_roots": 6,
        "local_tuning_cases": len(suite["cases"]),
        "status_counts": {
            status: sum(check["verification_status"] == status for check in checks)
            for status in sorted({check["verification_status"] for check in checks})
        },
        "selected_remote_fields": SELECT,
        "count_interpretation": (
            "LLM callbacks can include a request blocked before HTTP send; compare callback and HTTP counts separately. "
            "Terminal traces do not establish visual acceptance."
        ),
        "checks": checks,
    }


def main() -> None:
    """由 uv --env-file 注入现有追踪配置, 不加载、输出或保存凭据."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    directory = arguments.directory.resolve()
    with closing(Client(timeout_ms=(10000, 20000))) as client:
        result = verify(directory, client)
    path = directory / "trace-verification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temporary.replace(path)
    print(path)  # noqa: T201  # CLI 只输出核验报告路径.


if __name__ == "__main__":
    main()
