"""只允许完整读取安装包内的编排 skill, 并核对实际请求正文."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware
from langchain.tools import tool
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage


SKILL_PATH = "/skills/analysis-orchestration/SKILL.md"
SKILL_PROMPT = """可用编排 skill 由 SDK 从安装包发现:
{skills_locations}{skills_load_warnings}
{skills_list}
先调用 read_skill(path="/skills/analysis-orchestration/SKILL.md") 完整读取方法,
再依据下一次请求中的完整正文和工具回执继续业务操作。
"""


class OrchestrationSkill:
    """读取权限固定为一份文件, 完整回执进入请求后才获得业务权限."""

    def __init__(self) -> None:
        """读取稳定安装资源, SDK 与受限读取工具共用同一来源."""
        self.root = Path(str(files("shader_deep.resources")))
        self.content = (self.root / SKILL_PATH.lstrip("/")).read_text(encoding="utf-8")
        if not self.content.strip():
            msg = "编排 skill 正文为空"
            raise ValueError(msg)
        self.digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        self.loaded = False
        self.presented = False
        self.in_request = False
        self.backend = FilesystemBackend(root_dir=self.root, virtual_mode=True)
        self.middleware = SkillsMiddleware(backend=self.backend, sources=["/skills/"], system_prompt=SKILL_PROMPT)

    def tool(self) -> BaseTool:
        """只开放指定 skill 的完整读取, 无任意文件读取或截断参数."""

        @tool
        def read_skill(path: str) -> str:
            """完整读取可用编排 skill, 返回全文和内容指纹.

            Args:
                path: SDK 目录中给出的固定 SKILL.md 路径.
            """
            if path != SKILL_PATH:
                msg = "只允许读取本轮 packaged analysis-orchestration/SKILL.md"
                raise ValueError(msg)
            self.loaded = True
            return json.dumps({"status": "read", "path": SKILL_PATH, "sha256": self.digest, "content": self.content}, ensure_ascii=False)

        return read_skill

    def full_response(self, messages: list[BaseMessage]) -> bool:
        """只接受真实读取工具生成的完整回执, 文本提及路径不算已读."""
        if not self.loaded:
            return False
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name != "read_skill" or message.status == "error":
                continue
            try:
                payload = json.loads(message.content) if isinstance(message.content, str) else {}
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("sha256") == self.digest and payload.get("content") == self.content:
                return True
        return False

    def mark_presented(self, messages: list[BaseMessage]) -> None:
        """实际发送前登记完整 skill 已进入模型请求, 不使用目录发现代替正文."""
        self.presented = self.presented or self.full_response(messages)
