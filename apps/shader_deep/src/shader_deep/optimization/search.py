"""在固定试验预算内搜索归一化连续参数."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from random import Random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

Point = tuple[float, ...]
_COORDINATE_DIMENSIONS = 8
_MIN_STEP = 1.0 / 4096


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


@dataclass
class _Search:
    score: Callable[[Point], float]
    budget: int
    best: Point
    error: float = math.inf
    cache: dict[Point, float] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.budget - len(self.cache)

    def evaluate(self, point: Point) -> float:
        if point in self.cache:
            return self.cache[point]
        if not self.remaining:
            return math.inf
        loss = self.score(point)
        self.cache[point] = loss
        if loss < self.error:
            self.best, self.error = point, loss
        return loss


def _coordinates(search: _Search) -> None:
    step = 0.25
    while search.remaining and search.error and step >= _MIN_STEP:
        previous = search.error
        for axis in range(len(search.best)):
            center = search.best
            for direction in (-1, 1):
                value = _clip(center[axis] + direction * step)
                search.evaluate((*center[:axis], value, *center[axis + 1 :]))
        if search.error == previous:
            step *= 0.5


def _probe(search: _Search, random: Random, count: int) -> None:
    if count <= 0:
        return
    columns = []
    for initial in search.best:
        lower, upper = _clip(initial - 0.35), _clip(initial + 0.35)
        column = [lower + (upper - lower) * ((index + random.random()) / count) for index in range(count)]
        random.shuffle(column)
        columns.append(column)
    for point in zip(*columns, strict=True):
        search.evaluate(point)


def _identity(dimensions: int) -> list[list[float]]:
    return [[float(row == col) for col in range(dimensions)] for row in range(dimensions)]


def _diagonalize(covariance: list[list[float]]) -> tuple[list[list[float]], Point]:
    # 循环 Jacobi 旋转用于不超过 20 维的对称协方差, 避免新增线性代数依赖.
    size = len(covariance)
    basis, matrix = _identity(size), [row[:] for row in covariance]
    for _ in range(16):
        changed = False
        for a in range(size):
            for b in range(a + 1, size):
                offdiag = matrix[a][b]
                if abs(offdiag) <= 1e-12:  # noqa: PLR2004  # 该阈值远小于采样精度.
                    continue
                _rotate(matrix, basis, a, b, offdiag)
                changed = True
        if not changed:
            break
    scales = tuple(math.sqrt(max(matrix[i][i], 1e-12)) for i in range(size))
    return basis, scales


def _rotate(matrix: list[list[float]], basis: list[list[float]], a: int, b: int, offdiag: float) -> None:
    angle = 0.5 * math.atan2(2 * offdiag, matrix[b][b] - matrix[a][a])
    sine, cosine = math.sin(angle), math.cos(angle)
    for index, row in enumerate(basis):
        va, vb = row[a], row[b]
        row[a], row[b] = cosine * va - sine * vb, sine * va + cosine * vb
        if index in (a, b):
            continue
        ma, mb = matrix[index][a], matrix[index][b]
        matrix[index][a] = matrix[a][index] = cosine * ma - sine * mb
        matrix[index][b] = matrix[b][index] = sine * ma + cosine * mb
    da, db = matrix[a][a], matrix[b][b]
    matrix[a][a] = cosine**2 * da + sine**2 * db - 2 * sine * cosine * offdiag
    matrix[b][b] = sine**2 * da + cosine**2 * db + 2 * sine * cosine * offdiag
    matrix[a][b] = matrix[b][a] = 0.0


def _project(basis: list[list[float]], vector: Point) -> Point:
    return tuple(sum(a * b for a, b in zip(row, vector, strict=True)) for row in basis)


@dataclass(frozen=True)
class _Coefficients:
    weights: Point
    effective_count: float
    covariance_path_rate: float
    sigma_path_rate: float
    rank_one_rate: float
    rank_mu_rate: float
    damping: float
    expected_norm: float


def _coefficients(size: int, count: int) -> _Coefficients:
    ranks = tuple(math.log(count + 0.5) - math.log(rank) for rank in range(1, count + 1))
    weights = tuple(rank / sum(ranks) for rank in ranks)
    effective = 1 / sum(weight**2 for weight in weights)
    covariance = (4 + effective / size) / (size + 4 + 2 * effective / size)
    sigma = (effective + 2) / (size + effective + 5)
    rank_one = 2 / ((size + 1.3) ** 2 + effective)
    rank_mu = min(1 - rank_one, 2 * (effective - 2 + 1 / effective) / ((size + 2) ** 2 + effective))
    damping = 1 + 2 * max(0, math.sqrt((effective - 1) / (size + 1)) - 1) + sigma
    norm = math.sqrt(size) * (1 - 1 / (4 * size) + 1 / (21 * size**2))
    return _Coefficients(weights, effective, covariance, sigma, rank_one, rank_mu, damping, norm)


def _path(previous: Point, step: Point, rate: float, effective: float) -> Point:
    weight = math.sqrt(rate * (2 - rate) * effective)
    return tuple((1 - rate) * old + weight * new for old, new in zip(previous, step, strict=True))


def _rank_update(covariance: list[list[float]], path: Point, steps: tuple[Point, ...], rules: _Coefficients, correction: float) -> None:
    rank_one, rank_mu = rules.rank_one_rate, rules.rank_mu_rate
    retention = 1 - rank_one - rank_mu + rank_one * correction
    for a, row in enumerate(covariance):
        for b in range(a + 1):
            weighted = sum(weight * step[a] * step[b] for weight, step in zip(rules.weights, steps, strict=True))
            row[b] = covariance[b][a] = retention * row[b] + rank_one * path[a] * path[b] + rank_mu * weighted


@dataclass
class _Cma:
    center: Point
    covariance: list[list[float]]
    sigma_path: Point
    covariance_path: Point
    sigma: float = 0.22
    generations: int = 0

    def population(self, basis: list[list[float]], scales: Point, random: Random, count: int) -> tuple[Point, ...]:
        return tuple(self._sample(basis, scales, random) for _ in range(count))

    def _sample(self, basis: list[list[float]], scales: Point, random: Random) -> Point:
        noise = _project(basis, tuple(scale * random.gauss(0, 1) for scale in scales))
        return tuple(_clip(current + self.sigma * step) for current, step in zip(self.center, noise, strict=True))

    def update(self, elite: list[Point], basis: list[list[float]], scales: Point, rules: _Coefficients) -> None:
        self.generations += 1
        steps = tuple(tuple((value - center) / self.sigma for value, center in zip(point, self.center, strict=True)) for point in elite)
        step = tuple(sum(v * w for v, w in zip(axis, rules.weights, strict=True)) for axis in zip(*steps, strict=True))
        self.center = tuple(_clip(value + self.sigma * change) for value, change in zip(self.center, step, strict=True))
        # 沿协方差特征向量计算 C^(-1/2)·step, 域边界裁剪后的位移也用于更新分布.
        scaled = tuple(
            sum(v * w for v, w in zip(axis, step, strict=True)) / scale for axis, scale in zip(zip(*basis, strict=True), scales, strict=True)
        )
        whitened = _project(basis, scaled)
        self.sigma_path = _path(self.sigma_path, whitened, rules.sigma_path_rate, rules.effective_count)
        norm = math.hypot(*self.sigma_path) / rules.expected_norm
        keep_path = norm / math.sqrt(1 - (1 - rules.sigma_path_rate) ** (2 * self.generations)) < 1.4 + 2 / (len(self.center) + 1)
        rate = rules.covariance_path_rate
        self.covariance_path = _path(self.covariance_path, step if keep_path else (0.0,) * len(step), rate, rules.effective_count)
        _rank_update(self.covariance, self.covariance_path, steps, rules, 0.0 if keep_path else rate * (2 - rate))
        self.sigma = min(0.5, self.sigma * math.exp(rules.sigma_path_rate * (norm - 1) / rules.damping))


def _covariance_search(search: _Search) -> None:
    dimensions = len(search.best)
    size = 4 + int(3 * math.log(dimensions))
    random = Random(0)  # noqa: S311  # 固定种子仅用于可复验的数值搜索, 不用于安全用途.
    _probe(search, random, min(size, search.remaining // 3))
    empty = (0.0,) * dimensions
    cma = _Cma(search.best, _identity(dimensions), empty, empty)
    rules = _coefficients(dimensions, size // 2)
    for _ in range(search.budget):
        if not search.remaining or not search.error or cma.sigma < _MIN_STEP:
            break
        basis, scales = _diagonalize(cma.covariance)
        population = cma.population(basis, scales, random, min(size, search.remaining))
        ranked = sorted((search.evaluate(point), point) for point in dict.fromkeys(population))
        elite = [point for loss, point in ranked if math.isfinite(loss)][: len(rules.weights)]
        if len(elite) < len(rules.weights):
            # 保护区拒绝的候选不影响协方差, 拒绝过多时回到当前最优边界内缩步长.
            cma.center, cma.sigma = search.best, cma.sigma * 0.5
        else:
            cma.update(elite, basis, scales, rules)


def _minimize(score: Callable[[Point], float], origin: Point, budget: int) -> _Search:
    search = _Search(score, budget, origin)
    search.evaluate(origin)
    if len(origin) <= _COORDINATE_DIMENSIONS:
        _coordinates(search)
    else:
        _covariance_search(search)
    return search
