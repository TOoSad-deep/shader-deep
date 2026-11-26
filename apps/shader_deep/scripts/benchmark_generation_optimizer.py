"""从正式规划生成两组独立候选, 对照纯生成与开放数值优化时的实际结果.

在应用目录运行, 每个 --case 依次给出规划包、方案 ID、原图 ROI (左,上,右,下或 full):
uv run --no-sync --env-file .env python -m scripts.benchmark_generation_optimizer \
    --case /path/to/frost/planning S1 500,250,575,310 --case /path/to/glow/planning S1 full

命令使用当前配置的模型服务和真实 WebGL2。每种模式单独运行一次生成, 同组共享模型、
规划、原始像素画布、时间与预算。耗时包含输入捕获、模型与渲染, 使用量只列服务端有回执的
token。优化器是否被调用、是否产生改善和是否送审由实际运行决定。报告不代表视觉验收。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from time import perf_counter
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageStat

from shader_deep.agents.generation.options import GenerationOptions
from shader_deep.domain.visual_review import PixelRegion
from shader_deep.imaging.png16 import BIT_DEPTH, display_png16
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.infrastructure.storage.artifacts import create_run_directory
from shader_deep.infrastructure.storage.planning_inputs import reference_size
from shader_deep.infrastructure.storage.planning_package import read_planning_package
from shader_deep.infrastructure.storage.snapshots import write_snapshot
from shader_deep.optimization.objective import _display
from shader_deep.workflows.generation import _run_session
from shader_deep.workflows.generation_from_planning import _prepare_planning_generation

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from shader_deep.infrastructure.storage.planning_package import PlanningPackage
    from shader_deep.workflows.generation_from_planning import PreparedPlanningGeneration


@dataclass(frozen=True)
class _Case:
    package: PlanningPackage
    sketch: str
    reference: bytes
    size: tuple[int, int]
    region: PixelRegion


def _case(directory: str, sketch: str, coordinates: str) -> _Case:
    package = read_planning_package(Path(directory).resolve())
    if sketch not in {item.id for item in package.report.sketches}:
        msg = f"规划包没有方案 {sketch}"
        raise ValueError(msg)
    reference = package.source.reference_path.read_bytes()
    if hashlib.sha256(reference).hexdigest() != package.source.observation_manifest.reference.sha256:
        msg = "原图在正式规划读取后发生变化"
        raise ValueError(msg)
    w, h = reference_size(reference)
    left, top, right, bottom = (0, 0, w, h) if coordinates == "full" else map(int, coordinates.split(","))
    region = PixelRegion(left=left, top=top, right=right, bottom=bottom)
    if right > w or bottom > h:
        msg = f"ROI 超出参考图的 {w}x{h} 像素画布"
        raise ValueError(msg)
    return _Case(package, sketch, reference, (w, h), region)


def _pixels(content: bytes, case: _Case, stack: ExitStack) -> Image.Image:
    original = stack.enter_context(Image.open(io.BytesIO(content)))
    if original.size != case.size or original.format != "PNG":
        msg = "送审 PNG 与固定参考画布不一致"
        raise ValueError(msg)
    region = case.region
    box = (region.left, region.top, region.right, region.bottom)
    crop = stack.enter_context(display_png16(content, box) if content[24] == BIT_DEPTH else original.crop(box))
    return stack.enter_context(_display(crop))


def _error(case: _Case, png: bytes) -> float:
    with ExitStack() as stack:
        reference, rendered = _pixels(case.reference, case, stack), _pixels(png, case, stack)
        with ImageChops.difference(reference, rendered) as delta:
            return fmean(ImageStat.Stat(delta).mean) / 255


def _collect(directory: Path, result: dict[str, object], case: _Case) -> None:
    snapshot = json.loads((directory / "run.json").read_bytes())
    result.update(stop_reason=snapshot.get("stop_reason"), model_calls=snapshot.get("model_calls"), attempts=snapshot.get("attempts"))
    usage = snapshot.get("token_usage")
    if isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens", "total_tokens")):
        result["reported_tokens"] = usage
    outcomes = [json.loads(path.read_bytes()) for path in sorted(directory.glob("optimization-*.json"))]
    result["optimizer_statuses"] = [item.get("status") for item in outcomes]
    selected = snapshot.get("selected_candidate_id")
    result["selected_candidate_id"] = selected
    if not selected or snapshot.get("stop_reason") != "completed":
        return
    record = snapshot["blackboard"]["candidates"][selected]
    preview = Path(record["preview_path"]).resolve()
    if record["id"] != selected or not preview.is_relative_to(directory):
        msg = "所选候选的身份或预览路径与实际运行不符"
        raise ValueError(msg)
    image = preview.read_bytes()
    result.update(preview_path=str(preview), png_sha256=hashlib.sha256(image).hexdigest(), pixel_mae=_error(case, image))
    result["selected_optimizer_candidate"] = any(
        item.get("candidate_id") == selected or any(point["candidate_id"] == selected for point in item.get("turning_points", ()))
        for item in outcomes
    )


def _generate_fixed(case: _Case, prepared: PreparedPlanningGeneration, options: GenerationOptions, model: BaseChatModel) -> None:
    if prepared.inputs.binding.content_sha256 != case.package.content_sha256:
        msg = "输入规划包在两次采样之间发生变化"
        raise ValueError(msg)
    _run_session(prepared.state, "G1", Path.cwd(), options, run_dir=prepared.run_dir, inputs=prepared.inputs, model=model)


def _arm(case: _Case, options: GenerationOptions, model: BaseChatModel) -> dict[str, object]:
    result: dict[str, object] = {
        "optimize_parameters": options.optimize_parameters,
        "stop_reason": "error",
        "pixel_mae": None,
        "reported_tokens": None,
    }
    start = perf_counter()
    prepared = None
    try:
        prepared = _prepare_planning_generation(case.package.directory, case.sketch, options=options)
        _generate_fixed(case, prepared, options, model)
    except Exception as exc:  # noqa: BLE001  # 独立记录这一侧的失败, 继续测另一侧并保留实际用量.
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["generation_seconds"] = perf_counter() - start
    if prepared is not None:
        result["run_dir"] = str(prepared.run_dir)
        try:
            _collect(prepared.run_dir, result, case)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            result["artifact_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _compare(cases: list[_Case], model: BaseChatModel, directory: Path, *, attempts: int, calls: int) -> Path:
    results: list[dict[str, object]] = []
    report = {
        "model": getattr(model, "model_name", None),
        "max_attempts": attempts,
        "max_model_calls": calls,
        "pixel_mae_basis": "ROI 中白底合成 RGB 和原始 alpha 四通道逐像素 MAE, 按 255 归一化",
        "token_basis": "run.json 的服务端已回传用量汇总。缺少回执时可能只覆盖部分请求。整组缺失时为 null",
        "cases": results,
    }
    destination = directory / "comparison.json"
    for case in cases:
        source = case.package.source
        arms: list[dict[str, object]] = []
        results.append(
            {
                "planning_dir": str(case.package.directory),
                "planning_sha256": case.package.content_sha256,
                "sketch_id": case.sketch,
                "reference_sha256": hashlib.sha256(case.reference).hexdigest(),
                "render_size": case.size,
                "time": source.constraints.time,
                "roi": case.region.model_dump(mode="json"),
                "arms": arms,
            }
        )
        for enabled in (False, True):
            options = GenerationOptions(
                width=case.size[0],
                height=case.size[1],
                time=source.constraints.time,
                output_dir=directory / "runs",
                max_attempts=attempts,
                max_model_calls=calls,
                optimize_parameters=enabled,
            )
            arms.append(_arm(case, options, model))
            write_snapshot(destination, report)
    return destination


def main() -> None:
    """固定全部输入后执行有预算的成对生成, 打印 JSON 报告的绝对路径."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=3, action="append", metavar=("PLANNING_DIR", "SKETCH_ID", "ROI"), required=True)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--max-model-calls", type=int, default=8)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/generation-optimizer-benchmark"))
    args = parser.parse_args()
    cases = [_case(*entry) for entry in args.case]
    if args.max_attempts < 1 or args.max_model_calls < 1:
        parser.error("两侧预算都必须是正数")
    directory = create_run_directory(args.output_dir)
    report = _compare(cases, build_model(), directory, attempts=args.max_attempts, calls=args.max_model_calls)
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
