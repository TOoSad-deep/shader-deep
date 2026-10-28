"""分析工作流交付契约."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic import JsonValue

    from shader_deep.domain.five_libraries import (
        FiveLibraries,
        Issue,
    )


@dataclass(frozen=True, kw_only=True)
class FiveAnalysisResult:
    """交付读取同一选定版本, 失败没有伪造空五库包."""

    target_element_id: str | None
    libraries: FiveLibraries | None
    open_questions: tuple[Issue, ...] = ()
    gaps: tuple[Issue, ...] = ()
    status: Literal["completed", "partial", "failed"]
    # 版本选择只指向已接受的 V0/V1; 没有合法基线时与 libraries 一同保持为空.
    selected_version: str | None
    task_state: dict[str, JsonValue] | None = None
