"""保存不可变材料的集中索引, 提供受限目录、回读和原始像素裁剪."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from shader_deep.infrastructure.storage.snapshots import write_snapshot

MaterialKind = Literal["image", "code", "text", "json"]
MAX_PAGE_SIZE = 50


class MaterialError(ValueError):
    """携带可供工具逐项返回的稳定错误码."""

    def __init__(self, code: str, message: str) -> None:
        """记录错误码与可展示说明."""
        self.code = code
        super().__init__(message)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MemoryLimits(_Record):
    """限制一次读取展开量, 不决定原件是否保存."""

    max_text_chars: int = Field(default=32_768, gt=0)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=16_777_216, gt=0)


class MaterialRegion(_Record):
    """相对直接来源图像左上角的整数像素区域."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class MaterialOrigin(_Record):
    """调用方已核实的原运行、候选或报告身份, 缺失部分保持未知."""

    run_id: str | None = None
    material_id: str | None = None
    candidate_id: str | None = None
    task_id: str | None = None
    report_path: str | None = None
    content_sha256: str | None = None
    reference_sha256: str | None = None
    element_id: str | None = None
    sketch_id: str | None = None
    alternative: int | None = Field(default=None, ge=0)


class MaterialSource(_Record):
    """材料产生过程的引用, 不查询或复制业务状态."""

    type: Literal["input", "output", "derived"]
    task_id: str = Field(min_length=1)
    role: str | None = Field(default=None, min_length=1)
    operation: str | None = Field(default=None, min_length=1)
    candidate_id: str | None = Field(default=None, min_length=1)
    origin: MaterialOrigin | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """输入必须说明用途, 工具产物必须说明操作."""
        if (self.type == "input" and self.role is None) or (self.type != "input" and self.operation is None):
            msg = "输入材料需要 role, 工具产物需要 operation"
            raise ValueError(msg)
        return self


class MaterialDerivation(_Record):
    """裁剪的直接来源及原始坐标."""

    material_id: str = Field(pattern=r"^m[0-9]{4,}$")
    operation: Literal["crop"] = "crop"
    region: MaterialRegion


class _Description(_Record):
    """索引条目正文, 稳定 ID 由外层键提供."""

    kind: MaterialKind
    label: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: MaterialSource
    derived_from: MaterialDerivation | None = None

    @model_validator(mode="after")
    def validate_derivation(self) -> Self:
        """派生材料必须带来源, 且首版只产生裁剪图."""
        if (self.source.type == "derived") != (self.derived_from is not None):
            msg = "derived 来源与 derived_from 必须同时存在"
            raise ValueError(msg)
        if self.derived_from is not None and self.kind != "image":
            msg = "首版派生材料必须为图像"
            raise ValueError(msg)
        return self


class MaterialRecord(_Description):
    """一份材料的稳定身份; id 仅作为索引键保存."""

    id: str = Field(pattern=r"^m[0-9]{4,}$")


class _Index(_Record):
    version: int = Field(ge=1, le=1)
    run_id: str
    materials: dict[str, _Description]


class _Cursor(_Record):
    scope: str
    task: str
    kind: MaterialKind | None
    after: str


@dataclass(frozen=True, kw_only=True)
class MaterialContent:
    """校验后用于本轮呈现的内容, 不把图像编码写回索引."""

    record: MaterialRecord
    data: bytes
    text: str | None = None
    image_size: tuple[int, int] | None = None


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            msg = f"重复 JSON 键: {key}"
            raise MaterialError(code="invalid_index", message=msg)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    msg = f"JSON 不支持常量 {value}"
    raise ValueError(msg)


def _ordinal(identifier: str) -> int:
    if re.fullmatch(r"m[0-9]{4,}", identifier) is None or int(identifier[1:]) < 1:
        msg = "材料 ID 无效"
        raise MaterialError(code="invalid_index", message=msg)
    return int(identifier[1:])


class VisualMemory:
    """单运行材料访问对象; 写入由所属生成会话串行调用."""

    def __init__(self, run_dir: Path, task_id: str, *, limits: MemoryLimits | None = None) -> None:
        """绑定固定运行与当前任务, 已有索引仅恢复材料而不恢复模型会话.

        Args:
            run_dir: 本次运行的已分配目录.
            task_id: 当前可访问材料所属任务.
            limits: 本会话内容展开限制.
        """
        if not task_id.strip():
            msg = "task_id 不能为空"
            raise ValueError(msg)
        self.run_dir, self.task_id = run_dir.resolve(), task_id
        self.limits = limits or MemoryLimits()
        self.index_path = self._path("memory/index.json")
        self._records = self._load() if self.index_path.exists() else {}

    def __len__(self) -> int:
        """返回当前任务可访问的材料数量."""
        return sum(record.source.task_id == self.task_id for record in self._records.values())

    def _path(self, value: Path | str) -> Path:
        path = Path(value)
        resolved = (self.run_dir / path).resolve()
        if not resolved.is_relative_to(self.run_dir):
            msg = "材料路径必须位于本轮运行目录内"
            raise MaterialError(code="invalid_path", message=msg)
        return resolved

    def _load(self) -> dict[str, MaterialRecord]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            index = _Index.model_validate(raw)
            records = {key: MaterialRecord.model_validate({**value.model_dump(), "id": key}) for key, value in index.materials.items()}
        except (ValueError, TypeError, KeyError) as exc:
            msg = "材料索引格式、版本、来源引用或路径无效"
            raise MaterialError(code="invalid_index", message=msg) from exc
        if index.run_id != self.run_dir.name:
            msg = "材料索引不属于当前运行"
            raise MaterialError(code="invalid_index", message=msg)
        self._validate_records(records)
        return records

    def _validate_records(self, records: dict[str, MaterialRecord]) -> None:
        ordinals = [_ordinal(key) for key in records]
        if len(set(ordinals)) != len(ordinals):
            msg = "材料登记序号重复"
            raise MaterialError(code="invalid_index", message=msg)
        for record in records.values():
            if Path(record.path).is_absolute():
                msg = "索引中的材料路径必须为相对路径"
                raise MaterialError(code="invalid_index", message=msg)
            self._path(record.path)
            self._validate_parent(record, records)

    def _validate_parent(self, record: MaterialRecord, records: dict[str, MaterialRecord]) -> None:
        if record.derived_from is None:
            return
        parent = records.get(record.derived_from.material_id)
        if parent is None or parent.kind != "image" or parent.source.task_id != record.source.task_id or _ordinal(parent.id) >= _ordinal(record.id):
            msg = "裁剪来源不存在、类型错误或不属于当前任务的先前材料"
            raise MaterialError(code="invalid_index", message=msg)

    def get(self, material_id: str) -> MaterialRecord:
        """按 ID 获取本任务的描述, 不代表实际内容已呈现.

        Args:
            material_id: 本轮材料 ID.
        """
        record = self._records.get(material_id)
        if record is None or record.source.task_id != self.task_id:
            msg = "材料不存在或不可访问"
            raise MaterialError(code="unknown_material", message=msg)
        return record

    def register(
        self,
        path: Path | str,
        *,
        kind: MaterialKind,
        label: str,
        source: MaterialSource,
        derived_from: MaterialDerivation | None = None,
    ) -> MaterialRecord:
        """登记已保存文件; 索引成功发布前不改变可见材料.

        Args:
            path: 本轮内的绝对路径或相对路径.
            kind: 实际内容类别.
            label: 程序生成的名称.
            source: 调用方确认的来源.
            derived_from: 仅裁剪材料提供.
        """
        if source.task_id != self.task_id:
            msg = "不能登记其他任务的材料"
            raise MaterialError(code="invalid_source", message=msg)
        location = self._path(path)
        identifier = f"m{max((_ordinal(key) for key in self._records), default=0) + 1:04d}"
        record = MaterialRecord(
            id=identifier,
            kind=kind,
            label=label,
            path=location.relative_to(self.run_dir).as_posix(),
            sha256=hashlib.sha256(location.read_bytes()).hexdigest(),
            source=source,
            derived_from=derived_from,
        )
        self._validate_parent(record, self._records)
        self._publish({**self._records, identifier: record})
        return record

    def _publish(self, records: dict[str, MaterialRecord]) -> None:
        index = self._path("memory/index.json")
        index.parent.mkdir(parents=True, exist_ok=True)
        self._path("memory/index.tmp")
        payload = {
            "version": 1,
            "run_id": self.run_dir.name,
            "materials": {key: value.model_dump(mode="json", exclude={"id"}, exclude_none=True) for key, value in records.items()},
        }
        write_snapshot(index, payload)
        self._records = records

    def list_materials(self, *, kind: MaterialKind | None = None, limit: int = 20, cursor: str | None = None) -> dict[str, JsonValue]:
        """返回简短目录及下一页游标, 不展开文件或完整来源.

        Args:
            kind: 可选内容类别过滤.
            limit: 页大小, 范围为 1 至 50.
            cursor: 上一页返回的同运行、同过滤游标.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE or kind not in (None, "image", "code", "text", "json"):
            msg = "目录类别或页大小无效, limit 必须为 1 至 50"
            raise MaterialError(code="invalid_query", message=msg)
        after = self._cursor(cursor, kind)
        records = sorted(
            (
                item
                for item in self._records.values()
                if item.source.task_id == self.task_id and (kind is None or item.kind == kind) and _ordinal(item.id) > after
            ),
            key=lambda item: _ordinal(item.id),
        )
        page = records[:limit]
        next_cursor = self._encode_cursor(page[-1].id, kind) if len(records) > limit else None
        return {"materials": [self._summary(item) for item in page], "next_cursor": next_cursor}

    def _encode_cursor(self, identifier: str, kind: MaterialKind | None) -> str:
        cursor = _Cursor(scope=str(self.run_dir), task=self.task_id, kind=kind, after=identifier)
        return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode()

    def _cursor(self, cursor: str | None, kind: MaterialKind | None) -> int:
        if cursor is None:
            return 0
        try:
            query = _Cursor.model_validate_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
            record = self.get(query.after)
        except (ValueError, TypeError, binascii.Error) as exc:
            msg = "目录游标无效或不属于当前运行、任务和过滤条件"
            raise MaterialError(code="invalid_cursor", message=msg) from exc
        if (query.scope, query.task, query.kind) != (str(self.run_dir), self.task_id, kind) or (kind is not None and record.kind != kind):
            msg = "目录游标不属于当前运行、任务和过滤条件"
            raise MaterialError(code="invalid_cursor", message=msg)
        return _ordinal(query.after)

    @staticmethod
    def _summary(record: MaterialRecord) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"id": record.id, "kind": record.kind, "label": record.label}
        if record.source.candidate_id is not None:
            result["candidate_id"] = record.source.candidate_id
        if record.derived_from is not None:
            result["derived_from_id"] = record.derived_from.material_id
        return result

    def _bytes(self, record: MaterialRecord) -> bytes:
        try:
            data = self._path(record.path).read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            msg = "材料文件不存在"
            raise MaterialError(code="missing_file", message=msg) from exc
        if hashlib.sha256(data).hexdigest() != record.sha256:
            msg = "材料内容与登记哈希不一致"
            raise MaterialError(code="content_changed", message=msg)
        return data

    def read(self, material_id: str, *, region: MaterialRegion | None = None) -> MaterialContent:
        """回读校验后的完整材料, 或生成并回读指定裁剪.

        Args:
            material_id: 本轮可访问材料 ID.
            region: 仅图像支持, 坐标相对直接来源图像.
        """
        record = self.get(material_id)
        data = self._bytes(record)
        if region is not None:
            return self._crop(record, data, region)
        if record.kind == "image":
            size = self._image_size(data)
            self._check_image_bytes(data)
            return MaterialContent(record=record, data=data, image_size=size)
        return self._text(record, data)

    def _text(self, record: MaterialRecord, data: bytes) -> MaterialContent:
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "文本材料不是有效 UTF-8"
            raise MaterialError(code="invalid_content", message=msg) from exc
        if len(value) > self.limits.max_text_chars:
            msg = f"文本长度 {len(value)} 超过限制 {self.limits.max_text_chars}"
            raise MaterialError(code="content_too_large", message=msg)
        if record.kind == "json":
            try:
                json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            except ValueError as exc:
                msg = "JSON 材料格式无效"
                raise MaterialError(code="invalid_content", message=msg) from exc
        return MaterialContent(record=record, data=data, text=value)

    def _image_size(self, data: bytes) -> tuple[int, int]:
        try:
            # Pillow 在 open 阶段即可告警, 只在本次解码中将其转为可逐项返回的错误.
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    return self._validated_image_size(image)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            msg = f"图像超过安全解码限制: {exc}"
            raise MaterialError(code="content_too_large", message=msg) from exc
        except (UnidentifiedImageError, OSError) as exc:
            msg = "无法解码 PNG 材料"
            raise MaterialError(code="invalid_content", message=msg) from exc

    def _validated_image_size(self, image: Image.Image) -> tuple[int, int]:
        if image.format != "PNG":
            msg = "首版图像材料必须为 PNG"
            raise MaterialError(code="invalid_content", message=msg)
        if image.width * image.height > self.limits.max_image_pixels:
            msg = f"图像像素数 {image.width * image.height} 超过限制 {self.limits.max_image_pixels}"
            raise MaterialError(code="content_too_large", message=msg)
        image.load()
        return image.size

    def _check_image_bytes(self, data: bytes) -> None:
        if len(data) > self.limits.max_image_bytes:
            msg = f"图像字节数 {len(data)} 超过限制 {self.limits.max_image_bytes}"
            raise MaterialError(code="content_too_large", message=msg)

    def _crop(self, record: MaterialRecord, data: bytes, region: MaterialRegion) -> MaterialContent:
        if record.kind != "image":
            msg = "只有图像支持 region"
            raise MaterialError(code="invalid_region", message=msg)
        width, height = self._image_size(data)
        if region.x + region.width > width or region.y + region.height > height:
            msg = f"裁剪区域超出来源图像 {width} x {height}"
            raise MaterialError(code="invalid_region", message=msg)
        derivation = MaterialDerivation(material_id=record.id, region=region)
        existing = next((item for item in self._records.values() if item.source.task_id == self.task_id and item.derived_from == derivation), None)
        if existing is not None:
            return self.read(existing.id)
        return self._save_crop(record, data, derivation)

    def _save_crop(self, record: MaterialRecord, data: bytes, derivation: MaterialDerivation) -> MaterialContent:
        region = derivation.region
        with Image.open(io.BytesIO(data)) as image, image.crop((region.x, region.y, region.x + region.width, region.y + region.height)) as cropped:
            output = io.BytesIO()
            cropped.save(output, format="PNG")
        content = output.getvalue()
        self._check_image_bytes(content)
        identifier = f"m{max((_ordinal(key) for key in self._records), default=0) + 1:04d}"
        path = self._path(f"memory/derived/{identifier}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        source = MaterialSource(type="derived", task_id=self.task_id, operation="read_materials", candidate_id=record.source.candidate_id)
        label = f"{record.label} 局部 [{region.x}, {region.y}, {region.width}, {region.height}]"
        derived = self._publish_crop(path, content, label, source, derivation)
        return MaterialContent(record=derived, data=content, image_size=(region.width, region.height))

    def _publish_crop(self, path: Path, content: bytes, label: str, source: MaterialSource, derivation: MaterialDerivation) -> MaterialRecord:
        created = False
        try:
            with path.open("xb") as handle:
                created = True
                handle.write(content)
            return self.register(path, kind="image", label=label, source=source, derived_from=derivation)
        except BaseException as exc:
            # 仅清理本次新建且尚未登记的文件, 不删除碰撞路径或已有材料.
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup:
                    exc.add_note(f"未登记裁剪文件清理失败: {cleanup}")
            raise
