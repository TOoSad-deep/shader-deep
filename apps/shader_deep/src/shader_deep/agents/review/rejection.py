"""从受控字段提取 V2 工具校验诊断, 不归档模型原文或 SDK 错误正文."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from shader_deep.domain.visual_review import ReviewValidationError

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ToolCallRequest

    from shader_deep.workflows.review.session import ReviewSession

_TOOL_FIELDS = ("answer", "answers", "summary", "limitations", "reason", "evidence", "expected_revision")
_ANSWER_FIELDS = ("check_id", "rationale", "findings", "unassessed_reason")
_PATH_FIELDS = frozenset((*_TOOL_FIELDS, *_ANSWER_FIELDS, "region", "severity", "impact", "reference_observation", "rendered_observation"))
_MAX_PENDING_IDS = 8


def _shape(value: object) -> dict[str, object]:
    # 类型名也只来自 JSON 的基本类型, 避免任意对象控制诊断文本.
    kind = type(value)
    label = kind.__name__ if kind in (str, list, dict, bool, int, float, type(None)) else "other"
    return {"type": label, "length": len(value)} if isinstance(value, (str, list, dict)) else {"type": label}


def _arguments(args: dict[str, object], known_ids: set[str]) -> dict[str, object]:
    fields = {key: _shape(args[key]) for key in _TOOL_FIELDS if key in args}
    summary: dict[str, object] = {"fields": fields, "extra_field_count": len(args) - len(fields)}
    answer = args.get("answer")
    if isinstance(answer, dict):
        description = {key: _shape(answer[key]) for key in _ANSWER_FIELDS if key in answer}
        summary["answer_fields"] = description
        summary["extra_answer_field_count"] = len(answer) - len(description)
        check_id = answer.get("check_id")
        if isinstance(check_id, str) and check_id in known_ids:
            summary["check_id"] = check_id
    return summary


def _schema_path(request: ToolCallRequest) -> str:
    # SDK 的错误消息可能连着任意工具参数. 用本工具的纯输入 schema 还原字段位置.
    schema = request.tool.get_input_schema() if request.tool is not None else None
    if schema is not None and issubclass(schema, BaseModel):
        try:
            schema.model_validate(request.tool_call["args"])
        except ValidationError as error:
            first = error.errors(include_input=False, include_url=False, include_context=False)[0]
            fields = []
            for part in first["loc"]:
                if isinstance(part, str) and part not in _PATH_FIELDS:
                    break
                fields.append(str(part))
            if fields:
                return "/arguments/" + "/".join(fields)
    return "/arguments"


def rejection_diagnostic(session: ReviewSession, request: ToolCallRequest, error: ValueError | None) -> dict[str, object]:
    """给一次已注册工具的参数或领域拒绝生成小型、无自由文本的诊断.

    Args:
        session: 提供实际模型轮次和程序生成的检查项标识.
        request: 工具调用及其输入 schema.
        error: 领域或工具执行异常; SDK 转为错误消息时为空.

    Returns:
        可原子保存到本次运行目录的结构化快照.
    """
    known_ids = {check.check_id for check in session.checks}
    code, path = "invalid_tool_value", "/arguments"
    if isinstance(error, ReviewValidationError):
        # code 来自领域规则. path 中的未知检查项可能整段来自模型.
        code = error.code
        check_id = error.path.removeprefix("/answers/")
        path = f"/answers/{check_id}" if check_id in known_ids else "/answers"
    elif error is None:
        code, path = "invalid_tool_arguments", _schema_path(request)
    snapshot: dict[str, object] = {
        "tool": request.tool_call["name"],
        "model_call": session.model_calls,
        "code": code,
        "path": path,
        "arguments": _arguments(request.tool_call["args"], known_ids),
    }
    if code == "missing_answers":
        recorded = {answer.check_id for answer in session.check_answers}
        # 批次校验已确认这些 ID 合法. 诊断说明这次提交缺了哪些项, 其内容仍未登记.
        offered = request.tool_call["args"].get("answers", [])
        recorded.update(answer["check_id"] for answer in offered)
        missing = [check.check_id for check in session.checks if check.unassessed is None and check.check_id not in recorded]
        snapshot.update(missing_count=len(missing), missing_check_ids=missing[:_MAX_PENDING_IDS])
    return snapshot
