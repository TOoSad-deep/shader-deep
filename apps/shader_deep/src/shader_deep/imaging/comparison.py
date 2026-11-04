"""生成供视觉观察的并排图, 不计算误差或判断候选优劣."""

from io import BytesIO

from PIL import Image, ImageDraw, UnidentifiedImageError


def _decode_png(png: bytes) -> Image.Image:
    # 转为独立 RGBA 图后即可关闭输入流, 不依赖源文件或 Pillow 的延迟解码.
    try:
        with Image.open(BytesIO(png)) as image:
            if image.format != "PNG":
                msg = "Expected PNG image content"
                raise ValueError(msg)
            return image.convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        msg = "Unable to decode PNG image"
        raise ValueError(msg) from exc


def side_by_side_png(reference: bytes, current: bytes) -> bytes:
    """将等尺寸 PNG 原尺度并排放置, 标题位于图像区域之外.

    Args:
        reference: 左侧只读参考图的 PNG 内容.
        current: 右侧当前渲染图的 PNG 内容.

    Returns:
        带 24 像素标题栏的 RGBA PNG, 不裁剪、缩放或修改源图.

    Raises:
        ValueError: 内容不是可解码 PNG, 或两张图尺寸不同.
    """
    with _decode_png(reference) as left, _decode_png(current) as right:
        if left.size != right.size:
            msg = "Reference and current PNG dimensions must match; images are not resized"
            raise ValueError(msg)
        return _compose(left, right)


def _compose(left: Image.Image, right: Image.Image) -> bytes:
    width, height = left.size
    with Image.new("RGBA", (width * 2, height + 24), "white") as comparison:
        # 不传 alpha mask, 逐像素保留两张源图的 RGBA, 包括透明区域.
        comparison.paste(left, (0, 24))
        comparison.paste(right, (width, 24))
        draw = ImageDraw.Draw(comparison)
        draw.text((4, 4), "Reference", fill="black")
        draw.text((width + 4, 4), "Current", fill="black")
        with BytesIO() as output:
            comparison.save(output, format="PNG")
            return output.getvalue()
