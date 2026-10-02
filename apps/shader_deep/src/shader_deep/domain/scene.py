"""两个元素的整图组合要求和明确来源, 不读取文件或推断布局."""

from __future__ import annotations

from dataclasses import dataclass

SCENE_ELEMENT_COUNT = 2


@dataclass(frozen=True, kw_only=True)
class SceneElementSource:
    """一个已完成元素运行中明确选中的候选.

    Attributes:
        run_dir: 来源运行目录, 捕获时规范化为绝对路径.
        candidate_id: 来源运行实际选中的候选标识.
    """

    run_dir: str
    candidate_id: str

    def __post_init__(self) -> None:
        """拒绝没有明确来源的组合输入."""
        if not self.run_dir.strip() or not self.candidate_id.strip():
            msg = "Scene element requires a run directory and candidate ID"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class ScenePlan:
    """本次整图生成的要求及两个元素来源.

    Attributes:
        request: 用户要求的整图结果.
        background: 元素之外的背景要求.
        layout: 布局与遮挡约定, 可以使用保持原图的默认约定.
        elements: 两个明确选定的来源, 不表示将两份 shader 直接拼接.
    """

    request: str
    background: str
    layout: str
    elements: tuple[SceneElementSource, ...]

    def __post_init__(self) -> None:
        """只校验组合必需字段和来源数量, 不访问源运行."""
        if not self.request.strip() or not self.background.strip() or not self.layout.strip():
            msg = "Scene request, background and layout must not be empty"
            raise ValueError(msg)
        if len(self.elements) != SCENE_ELEMENT_COUNT:
            msg = "Scene generation requires exactly two element sources"
            raise ValueError(msg)
        if len(set(self.elements)) != len(self.elements):
            msg = "Scene element sources must be distinct"
            raise ValueError(msg)
