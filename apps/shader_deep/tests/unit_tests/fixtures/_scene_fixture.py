"""同一原图两个元素的报告及离线来源夹具, 短 ID 故意重复."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.blackboard import add_candidate
from shader_deep.domain.scene import SceneElementSource
from shader_deep.domain.tasks import CandidateRecord
from shader_deep.infrastructure.storage.artifacts import save_run
from shader_deep.infrastructure.storage.report_package import ReferenceAsset, ReportManifest, write_report_package
from shader_deep.workflows.generation_from_report import _prepare_generation
from tests.unit_tests.infrastructure.test_report_package import package_libraries

CIRCLE_CODE = """void mainImage(out vec4 c, in vec2 p) {
    vec2 uv = p / iResolution.xy;
    c = vec4(0.1, 0.1, 0.1, 1.0);
    if (length(uv - vec2(0.4, 0.5)) < 0.3) c = vec4(1.0, 0.0, 0.0, 1.0);
}"""
RECTANGLE_CODE = """void mainImage(out vec4 c, in vec2 p) {
    vec2 uv = p / iResolution.xy;
    c = vec4(0.1, 0.1, 0.1, 1.0);
    if (all(lessThan(abs(uv - vec2(0.65, 0.5)), vec2(0.2)))) c = vec4(0.0, 1.0, 0.0, 1.0);
}"""
SCENE_CODE = """void mainImage(out vec4 c, in vec2 p) {
    vec2 uv = p / iResolution.xy;
    c = vec4(0.1, 0.1, 0.1, 1.0);
    if (length(uv - vec2(0.4, 0.5)) < 0.3) c = vec4(1.0, 0.0, 0.0, 1.0);
    if (all(lessThan(abs(uv - vec2(0.65, 0.5)), vec2(0.2)))) c = vec4(0.0, 1.0, 0.0, 1.0);
}"""


def scene_reports(root: Path) -> tuple[Path, Path]:
    """生成同一完整原图的两个报告, 每份包都使用 E1/F1/M1/S1."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "scene-reference.png"
    with Image.new("RGB", (32, 24), (26, 26, 26)) as image:
        draw = ImageDraw.Draw(image)
        draw.ellipse((3, 5, 22, 19), fill=(255, 0, 0))
        draw.rectangle((15, 7, 26, 16), fill=(0, 255, 0))
        image.save(path)
    reference = path.read_bytes()
    manifest = ReportManifest(reference=ReferenceAsset(sha256=hashlib.sha256(reference).hexdigest()), target_element_id="E1", status="completed")
    reports = []
    for index, (name, region) in enumerate((("红色圆形", "左侧圆形本体"), ("绿色方形", "右侧遮挡圆形的方形")), start=1):
        library = package_libraries()
        element = library.elements[0].model_copy(update={"name": name, "region": region})
        feature = library.features[0].model_copy(update={"description": f"{name}的轮廓与纯色"})
        mechanism = library.mechanisms[0].model_copy(update={"name": f"{name}遮罩", "method": f"在完整画布坐标中构造{name}的距离遮罩"})
        library = library.model_copy(
            update={"elements": (element,), "features": (feature, library.features[1]), "mechanisms": (mechanism, library.mechanisms[1])}
        )
        reports.append(write_report_package(root / f"report-{index}", library, manifest, reference))
    return reports[0], reports[1]


def scene_sources(root: Path) -> tuple[SceneElementSource, ...]:
    """构造已选来源记录供无网络测试; 不将这些记录当作真实浏览器验证."""
    sources = []
    reports = scene_reports(root)
    for report, code in zip(reports, (CIRCLE_CODE, RECTANGLE_CODE), strict=True):
        prepared = _prepare_generation(report, "S1", "实现所选元素", background="深灰", options=GenerationOptions(output_dir=root / "source-runs"))
        code_path, preview_path = prepared.run_dir / "selected.glsl", prepared.run_dir / "selected.png"
        code_path.write_text(code, encoding="utf-8")
        preview_path.write_bytes(Path(prepared.inputs.reference_path).read_bytes())
        candidate = CandidateRecord(id="C1", task_id=prepared.task_id, code_path=str(code_path), preview_path=str(preview_path))
        state = add_candidate(prepared.state, candidate)
        save_run(prepared.run_dir, state, {"task_id": prepared.task_id, "stop_reason": "completed", "selected_candidate_id": candidate.id})
        sources.append(SceneElementSource(run_dir=str(prepared.run_dir), candidate_id=candidate.id))
    return tuple(sources)
