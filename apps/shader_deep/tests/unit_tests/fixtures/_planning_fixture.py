"""整图规划到生成的固定输入夹具."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from PIL import Image

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.scene_observations import ObservationIssue, build_scene_observations
from shader_deep.domain.scene_planning import ScenePlanningSubmission
from shader_deep.infrastructure.storage.observation_package import ObservationManifest, ObservationReference, write_observation_package
from shader_deep.infrastructure.storage.planning_inputs import capture_planning_source
from shader_deep.infrastructure.storage.planning_package import write_planning_package
from tests.unit_tests.domain.test_scene_observations import scene_submission

if TYPE_CHECKING:
    from pathlib import Path


def planning_report(root: Path, *, gaps: bool = False, color: tuple[int, int, int, int] = (160, 180, 210, 255)) -> Path:
    """保存真实 PNG、两元素观察及含局部备选的正式规划包."""
    root.mkdir(parents=True, exist_ok=True)
    image_path = root / "planning-reference.png"
    with Image.new("RGBA", (32, 24), color) as image:
        image.save(image_path)
    reference = image_path.read_bytes()
    observation = scene_submission()
    upstream = (ObservationIssue(description="必要全图细节仍缺失"),) if gaps else ()
    manifest = ObservationManifest(
        reference=ObservationReference(sha256=hashlib.sha256(reference).hexdigest()),
        status="partial" if gaps else "completed",
        context_notes=observation.context_notes,
        open_questions=observation.open_questions,
        gaps=upstream,
    )
    directory = write_observation_package(root / "observations", build_scene_observations(observation), manifest, reference)
    source = capture_planning_source(
        directory, root / "planning-source", "复现两个圆及交叠外观", generation_options=GenerationOptions(width=40, height=30, time=1.5)
    )
    report = ScenePlanningSubmission.model_validate(
        {
            "mechanisms": [
                {"id": "M1", "name": "圆形遮罩", "method": "按中心与半径求距离并输出覆盖率, 边缘宽度可调."},
                {"id": "M2", "name": "方向色场", "method": "将局部位置投影后插值颜色, 输出连续色场."},
                {"id": "M3", "name": "底层柔化", "method": "多点求值底层颜色并加权, 输出柔化底色.", "conditions": ["底层颜色可独立重复求值"]},
            ],
            "sketches": [
                {
                    "id": "S1",
                    "name": "解析整图方案",
                    "default": {"F8": ["M2"], "F3": ["M1"], "F15": ["M1"], "R9": ["M3"], "R2": ["M1", "M2"]},
                    "composition": "以 [[M1]]、[[M2]] 构造 [[E2]] 的底层色场, 再用 [[M1]] 定位 [[E7]], 在交叠区用 [[M3]] 柔化底层.",
                    "gaps": [{"refs": ["R9"], "description": "交叠区着色方法尚缺"}] if gaps else [],
                    "alternatives": [
                        {
                            "choices": {"R9": ["M1", "M2"]},
                            "reason": "简单背景可用宽边遮罩近似柔化",
                            "composition": "以 [[M1]]、[[M2]] 构造两圆并在前圆区域使用宽过渡近似 [[R9]]。",
                        }
                    ],
                }
            ],
        }
    )
    return write_planning_package(root / "planning-report", source, report)
