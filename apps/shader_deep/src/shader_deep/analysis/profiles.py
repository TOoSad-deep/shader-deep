"""压缩数值证据, 并附原图坐标, 减少模型手动数数组下标的需要."""

from __future__ import annotations

from shader_deep.analysis.evidence import ProfileDigest, ProfilePoint

SAMPLE_COUNT = 33
EXTREMA_LIMIT = 16
RADIUS = 3
MINIMUM_CONTRAST = 1.0


def summarize_profile(values: tuple[float, ...], offset: int) -> ProfileDigest:
    """定位有对比度的极值, 并提取均匀采样点, 均使用原图像素坐标.

    Args:
        values: 沿原图连续像素计算的编码亮度近似值.
        offset: 首个采样点在所选原图坐标轴上的位置.

    Returns:
        数量受限的采样和极值候选; 极值不自动解释为网格线.

    Raises:
        ValueError: 剖面数据为空.
    """
    count = len(values)
    if not count:
        msg = "A profile must contain at least one value"
        raise ValueError(msg)
    # 在整个剖面上均匀选点并包含两端; 短剖面用集合去除舍入后重合的位置.
    indices = sorted({round(index * (count - 1) / (SAMPLE_COUNT - 1)) for index in range(SAMPLE_COUNT)})
    dark, bright = _extrema(values, offset, -1), _extrema(values, offset, 1)
    return ProfileDigest(
        count=count,
        samples=tuple(ProfilePoint(position=offset + index, value=values[index]) for index in indices),
        dark_extrema=_strongest(dark),
        bright_extrema=_strongest(bright),
        extrema_truncated=max(len(dark), len(bright)) > EXTREMA_LIMIT,
        radius_pixels=RADIUS,
        minimum_contrast=MINIMUM_CONTRAST,
    )


def _extrema(values: tuple[float, ...], offset: int, polarity: int) -> list[ProfilePoint]:
    """按亮峰或暗谷的极性筛选双侧极值, 将连续合格点合并为一个候选."""
    groups: list[list[ProfilePoint]] = [[]]
    for index in range(RADIUS, len(values) - RADIUS):
        neighbors = (values[index - RADIUS], values[index + RADIUS])
        # 亮峰与较亮邻点比, 暗谷与较暗邻点比: 对比度取两侧方向差中较小的那个.
        # 这只描述半径范围内的局部差异, 不等同于物体相对无遮挡背景的对比度.
        baseline = max(neighbors) if polarity > 0 else min(neighbors)
        contrast = round(polarity * (values[index] - baseline), 3)
        if contrast >= MINIMUM_CONTRAST and _is_extremum(values, index, polarity):
            groups[-1].append(ProfilePoint(position=offset + index, value=values[index], contrast=contrast, neighbor_level=round(baseline, 3)))
        elif groups[-1]:
            groups.append([])
    return [max(group, key=lambda point: point.contrast) for group in groups if group]


def _is_extremum(values: tuple[float, ...], index: int, polarity: int) -> bool:
    """要求中心在左右邻域均形成极值, 排除单调斜坡和平坦平台."""
    center = polarity * values[index]
    left = [polarity * value for value in values[index - RADIUS : index]]
    right = [polarity * value for value in values[index + 1 : index + RADIUS + 1]]
    # 仅用二阶差分, 会把平坦或倾斜背景上的暗线两侧误判为额外极值.
    # 因此还要求中心相对左右邻域都构成真实的峰或谷.
    return center >= max(*left, *right) and center > min(left) and center > min(right)


def _strongest(points: list[ProfilePoint]) -> tuple[ProfilePoint, ...]:
    """先按对比度保留最强候选, 再按原图位置排序以便阅读."""
    selected = sorted(points, key=lambda point: point.contrast, reverse=True)[:EXTREMA_LIMIT]
    return tuple(sorted(selected, key=lambda point: point.position))
