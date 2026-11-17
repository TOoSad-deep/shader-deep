"""将共用 SDK 草稿后端绑定到观察会话文件."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.agents.draft_runtime import DraftBackend as SharedDraftBackend

if TYPE_CHECKING:
    from shader_deep.workflows.analysis.session import AnalysisSession


class DraftBackend(SharedDraftBackend):
    """保留观察后端入口, 使用同一会话草稿."""

    def __init__(self, session: AnalysisSession) -> None:
        """固定观察草稿文件名."""
        super().__init__(session, filename="observations.json")
