"""五库分析的终端进度与文本日志, 不承载业务状态或模型材料."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TextIO

    from pydantic import JsonValue

LOGGER_NAME = "shader_deep.analysis"
LOGGER = logging.getLogger(LOGGER_NAME)
LOGGER.addHandler(logging.NullHandler())
LOG_LOCK = Lock()
FORMATTER = logging.Formatter(
    "%(asctime)sZ %(levelname)s [%(run_id)s/%(task_id)s attempt=%(attempt_id)s call=%(model_call)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
FORMATTER.converter = time.gmtime
SECRET_FIELDS = {
    "api_key",
    "authorization",
    "openrouter_api_key",
    "micu_api_key",
    "ds_micu_api_key",
    "reasoning",
    "reasoning_details",
    "reasoning_content",
}
DATA_URL = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=_-]+")
API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]+")


def _safe(value: JsonValue) -> JsonValue:
    """移除凭据、图片载荷与推理字段, 其余业务文本保留原样."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SECRET_FIELDS else _safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_safe(child) for child in value]
    if isinstance(value, str):
        return BEARER.sub("Bearer [REDACTED]", API_KEY.sub("[REDACTED]", DATA_URL.sub("[IMAGE DATA]", value)))
    return value


def log_analysis(
    directory: Path,
    message: str,
    *,
    task_id: str = "workflow",
    attempt_id: str = "-",
    model_call: int = 0,
    level: int = logging.INFO,
    details: dict[str, JsonValue] | None = None,
) -> None:
    """保存完整诊断并按宿主级别输出, 写日志失败不改变任务结果.

    Args:
        directory: 本轮运行目录.
        message: 步骤说明.
        task_id: 逻辑任务身份.
        attempt_id: 当前执行实例.
        model_call: 逻辑模型调用序号.
        level: 终端过滤级别; 文件保留全部级别.
        details: 业务诊断正文, 不包含完整模型请求或客户端配置.
    """
    message = str(_safe(message))
    if details:
        message += "\n" + json.dumps(_safe(details), ensure_ascii=False, indent=2)
    identity = {"run_id": directory.name, "task_id": task_id, "attempt_id": attempt_id, "model_call": model_call or "-"}
    record = LOGGER.makeRecord(LOGGER_NAME, level, __file__, 0, message, (), None, extra=identity)
    try:
        with LOG_LOCK, (directory / "analysis.log").open("a", encoding="utf-8") as stream:
            stream.write(FORMATTER.format(record) + "\n")
    except OSError as error:
        LOGGER.warning("无法写入分析文本日志: %s", error, extra=identity)
    LOGGER.log(level, message, extra=identity)


@contextmanager
def analysis_console(stream: TextIO, level: str) -> Iterator[None]:
    """临时配置 CLI stderr, 退出后恢复宿主日志设置.

    Args:
        stream: 标准错误流.
        level: 终端诊断级别.

    Yields:
        已配置终端日志的执行范围.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(FORMATTER)
    handler.setLevel(level)
    previous_level, previous_propagation = LOGGER.level, LOGGER.propagate
    LOGGER.setLevel(level)
    LOGGER.propagate = False
    LOGGER.addHandler(handler)
    try:
        yield
    finally:
        LOGGER.removeHandler(handler)
        handler.close()
        LOGGER.setLevel(previous_level)
        LOGGER.propagate = previous_propagation
