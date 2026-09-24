"""执行组件所需的最小只读配置协议."""

from typing import Protocol


class ModelOptions(Protocol):
    """模型传输仅使用网络和输出字段."""

    @property
    def request_timeout_seconds(self) -> int:
        """单次网络尝试的超时秒数."""
        ...

    @property
    def stream_model_responses(self) -> bool:
        """是否使用完整接收的流式响应."""
        ...

    @property
    def max_output_tokens(self) -> int:
        """本轮输出预算."""
        ...


class RequestOptions(ModelOptions, Protocol):
    """完整请求与材料预算所需字段."""

    @property
    def request_image_tokens(self) -> int:
        """每图的保守估算预算."""
        ...

    @property
    def request_token_margin(self) -> int:
        """编码与估算余量."""
        ...

    @property
    def max_context_tokens(self) -> int:
        """完整请求预算."""
        ...

    @property
    def integration_history_tokens(self) -> int:
        """修复历史预留."""
        ...
