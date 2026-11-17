"""直接保存服务端工具参数, 对比单字符正则与无正则的字符串 Schema."""

import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

for flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
    os.environ[flag] = "false"

from shader_deep.infrastructure.llm.client import build_model

ROOT = Path(__file__).resolve().parent

if urlsplit(os.environ.get("OPENROUTER_BASE_URL", "")).hostname != "openrouter.ai":
    raise SystemExit("目的地不符合授权")
model = build_model()
client = model.root_client.with_options(max_retries=0, timeout=120)
results = []
for label, pattern in (("current_pattern", r"\S"), ("no_pattern", None), ("current_pattern_repeat", r"\S"), ("no_pattern_repeat", None)):
    string = {"type": "string", "minLength": 1}
    if pattern is not None:
        string["pattern"] = pattern
    schema = {"type": "object", "properties": {"label": string, "description": string}, "required": ["label", "description"], "additionalProperties": False}
    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model.model_name,
            messages=[{"role": "user", "content": "调用 emit_probe 工具。label 必须完整填写 alpha123，description 必须完整填写 霓虹圆角边框。不要缩写，不要只写首字符。"}],
            tools=[{"type": "function", "function": {"name": "emit_probe", "description": "保存两个完整字符串。", "strict": True, "parameters": schema}}],
            tool_choice={"type": "function", "function": {"name": "emit_probe"}},
            max_tokens=4096,
            stream=False,
            extra_body=dict(model.extra_body or {}),
        )
        choice = response.choices[0]
        result = {"variant": label, "schema": schema, "finish_reason": choice.finish_reason,
                  "raw_arguments": [call.function.arguments for call in choice.message.tool_calls or []],
                  "usage": response.usage.model_dump() if response.usage else None,
                  "response_model": response.model}
    except Exception as error:
        result = {"variant": label, "error_type": type(error).__name__, "error": str(error)}
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    results.append(result)
    (ROOT / "schema-probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)
