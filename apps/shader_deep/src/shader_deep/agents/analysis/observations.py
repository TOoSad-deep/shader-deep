"""绑定单任务原图的观察工具与真实图像回读门禁."""

# 取证链为 tools 生成记录 → context 放入请求 → mark_presented 登记 → snapshot 随报告冻结.
# 门禁只证明材料已进入模型请求, 不证明模型理解了图像或结论在视觉上正确.

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from langchain.tools import tool
from pydantic import JsonValue, TypeAdapter

from shader_deep.domain.evidence import ImageRegion, MeasurementRecord, MeasurementRequest, MeasurementSpec
from shader_deep.imaging.measurements import ReferenceMeasurements
from shader_deep.infrastructure.llm.messages import png_data_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain.tools import BaseTool
    from langchain_core.messages import BaseMessage, HumanMessage

    from shader_deep.domain.five_libraries import ExplorationSubmission


class ReferenceObservations:
    """显式取证或目标框裁剪时创建; 每个角色拥有独立预算与制品目录."""

    def __init__(
        self,
        reference_url: str,
        directory: Path,
        task_id: str,
        limit: int,
        *,
        target_box: ImageRegion | None = None,
        target_version: str = "T1",
        active: Callable[[], bool] | None = None,
    ) -> None:
        """固定原图并检查输入框, 自动留白裁剪不扣按需测量额度."""
        # 测量器持有本轮原图字节与哈希, 后续操作不重新读取可变的原始文件路径.
        data = base64.b64decode(reference_url.split(",", 1)[1], validate=True)
        directory.mkdir(parents=True, exist_ok=True)
        self.measurements = ReferenceMeasurements(data, directory, task_id, target_version, limit)
        self.active = active
        self._lock = RLock()
        self.presented: set[str] = set()
        self.asset_contents: dict[str, str] = {}
        self.focus: dict[str, JsonValue] = {}
        self.focus_url: str | None = None
        if target_box is not None:
            self._focus(target_box)

    def _focus(self, box: ImageRegion) -> None:
        width, height = self.measurements.image_size
        if box.right > width or box.bottom > height:
            msg = "目标框超出固定原图边界"
            raise ValueError(msg)
        margin = max(1, round(max(box.right - box.left, box.bottom - box.top) * 0.1))
        # 聚焦图保留少量周边供定位; 坐标与 offset 仍指向原图, 不改写目标框.
        region = ImageRegion(
            left=max(0, box.left - margin),
            top=max(0, box.top - margin),
            right=min(width, box.right + margin),
            bottom=min(height, box.bottom + margin),
        )
        spec = MeasurementSpec(kind="crop", region=region)
        # 自动聚焦直接裁剪固定字节, 不登记证据 ID, 也不消耗按需测量预算.
        with self.measurements._pixels(spec) as pixels:
            path = self.measurements.directory / "focus.png"
            pixels.save(path)
        self.focus_url = png_data_url(path)
        self.focus = {
            "target_box": asdict(box),
            "region": asdict(region),
            "offset": [region.left, region.top],
            "image_size": [width, height],
            "image_sha256": self.measurements.image_sha256,
        }

    def context(self, message: HumanMessage) -> HumanMessage:
        """把全图之外的裁剪与数值记录放入实际多模态请求."""
        # 工具回执只含元数据; 真正可看的裁剪字节在后续模型请求中补入.
        content = [{"type": "text", "text": message.content}] if isinstance(message.content, str) else list(message.content)
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {"focus": self.focus, "evidence": [asdict(item) for item in self.measurements.records.values()]}, ensure_ascii=False
                ),
            }
        )
        urls = ([self.focus_url] if self.focus_url else []) + [
            self._asset_url(item.artifact_path) for item in self.measurements.records.values() if item.artifact_path
        ]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
        return message.model_copy(update={"content": content})

    def _asset_url(self, path: str) -> str:
        """已呈现的字节保留在内存, 不跟随临时文件变化."""
        content = self.asset_contents.get(path)
        if content is None:
            content = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        return "data:image/png;base64," + content

    def mark_presented(self, messages: list[BaseMessage]) -> None:
        """仅实际请求中含记录及裁剪字节时登记呈现, 工具返回不算已看."""
        encoded = json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)
        for record in self.measurements.records.values():
            # 数值记录无需附图; crop 则要求记录 ID 与对应 data URL 同时出现在请求中.
            image_seen = not record.artifact_path or self._asset_url(record.artifact_path) in encoded
            if record.id in encoded and image_seen:
                self.presented.add(record.id)
                if record.artifact_path:
                    # 首次呈现后保存该份字节, 回读与交付不再跟随临时 PNG 的后续变化.
                    self.asset_contents.setdefault(record.artifact_path, self._asset_url(record.artifact_path).split(",", 1)[1])

    def validate(self, report: ExplorationSubmission) -> None:
        """拒绝伪造、串用、未回读的证据及非 F/R 关联."""
        known = {item.id for item in self.measurements.records.values()}
        observations = {item.id for item in (*report.features, *report.relations)}
        if not report.evidence_links.keys() <= observations:
            msg = "证据只能关联当前任务的 F/R"
            raise ValueError(msg)
        for ids in report.evidence_links.values():
            if not ids or len(set(ids)) != len(ids) or not set(ids) <= known & self.presented:
                msg = "证据必须由当前任务实际生成并在后续请求呈现"
                raise ValueError(msg)

    def snapshot(self, report: ExplorationSubmission) -> dict[str, JsonValue]:
        """冻结已实际呈现且由当前报告引用的程序证据."""
        # 与新测量共用锁, 避免校验、选取记录和收集制品期间出现新的测量写入.
        with self._lock:
            self.validate(report)
            used = {identity for ids in report.evidence_links.values() for identity in ids}
            assets = self._published_assets(used)
            # 只发布本报告引用的材料; 哈希对应已呈现的内存字节, 而非再次读取文件.
            return TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "records": [
                        TypeAdapter(MeasurementRecord).dump_python(record, mode="json")
                        for record in self.measurements.records.values()
                        if record.id in used
                    ],
                    "links": {key: list(ids) for key, ids in report.evidence_links.items()},
                    "asset_contents": assets,
                    "assets": {path: hashlib.sha256(base64.b64decode(content)).hexdigest() for path, content in assets.items()},
                }
            )

    def _published_assets(self, used: set[str]) -> dict[str, str]:
        """将实际呈现的制品随不可变版本持久保存, 不依赖角色临时目录."""
        return {
            record.artifact_path: self.asset_contents[record.artifact_path]
            for record in self.measurements.records.values()
            if record.id in used and record.artifact_path
        }

    def tools(self) -> list[BaseTool]:
        """只开放原图裁剪和区域统计, 无路径输入."""

        @tool
        def inspect_reference(kind: Literal["crop", "region_stats"], region: ImageRegion, question: str) -> str:
            """围绕具体疑问查看原图区域或统计; 下一轮才可引用结果.

            Args:
                kind: crop 返回原始像素裁剪, region_stats 采用白底合成区域均值.
                region: 原图整数像素框, 右下边界不包含, 须在图内.
                question: 此次操作需要确认的非空问题.
            """
            # SDK 可并发执行同批工具, 取证 ID、缓存与额度必须在同一锁内变更.
            with self._lock:
                if self.active is not None and not self.active():
                    msg = "当前执行实例不再接受测量操作"
                    raise ValueError(msg)
                record, cached = self.measurements.measure(
                    MeasurementRequest(question=question, measurement=MeasurementSpec(kind=kind, region=region))
                )
            return json.dumps({"record": asdict(record), "cached": cached}, ensure_ascii=False)

        return [inspect_reference]
