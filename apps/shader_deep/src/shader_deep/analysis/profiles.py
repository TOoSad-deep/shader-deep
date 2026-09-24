"""兼容旧导入路径; 实现位于 imaging.profiles."""

from shader_deep.imaging.profiles import (
    EXTREMA_LIMIT as EXTREMA_LIMIT,
    MINIMUM_CONTRAST as MINIMUM_CONTRAST,
    RADIUS as RADIUS,
    SAMPLE_COUNT as SAMPLE_COUNT,
    _extrema as _extrema,
    _is_extremum as _is_extremum,
    _strongest as _strongest,
    summarize_profile as summarize_profile,
)
