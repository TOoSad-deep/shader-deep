"""独立视觉评审角色的预算与传输参数."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class ReviewOptions:
    """评审调用、输出及独立运行目录配置.

    Attributes:
        max_model_calls: 主模型节点的有限调用预算.
        max_request_retries: 暂时性传输错误的额外请求次数.
        request_timeout_seconds: 连接、读取、写入和连接池等待操作的超时秒数.
        stream_model_responses: 使用完整接收并校验的流式响应.
        max_output_tokens: 每次模型请求的输出 token 预算.
        output_dir: 独立评审运行目录的父目录.
    """

    max_model_calls: int = 3
    max_request_retries: int = 2
    request_timeout_seconds: int = 240
    stream_model_responses: bool = True
    max_output_tokens: int = 8192
    output_dir: Path | None = None

    def __post_init__(self) -> None:
        """在创建模型或运行目录前检查容量与类型."""
        for name in ("max_model_calls", "request_timeout_seconds", "max_output_tokens", "max_request_retries"):
            value = getattr(self, name)
            minimum = 0 if name == "max_request_retries" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                msg = f"{name} must be an integer >= {minimum}"
                raise ValueError(msg)
        if not isinstance(self.stream_model_responses, bool):
            msg = "stream_model_responses must be a boolean"
            raise TypeError(msg)
        if self.output_dir is not None and not isinstance(self.output_dir, Path):
            msg = "output_dir must be a Path or null"
            raise TypeError(msg)
