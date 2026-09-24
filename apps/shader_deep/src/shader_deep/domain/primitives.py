"""业务契约共用的非空文本与严格字段配置."""

from typing import Annotated

from pydantic import ConfigDict, StringConstraints

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
CONFIG = ConfigDict(extra="forbid")
