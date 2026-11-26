"""按观察依赖限定一次规划修订可修改的范围."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shader_deep.domain.local_probe import probe_observation_dependencies
from shader_deep.domain.visual_review import PixelRegion

if TYPE_CHECKING:
    from shader_deep.domain.scene_observations import ReferenceMeasurement, SceneObservations
    from shader_deep.domain.scene_planning import ObservationRevisionRequest, PlanningSketch, ScenePlanningSubmission
    from shader_deep.infrastructure.storage.observation_package import ObservationManifest


@dataclass(frozen=True)
class ObservationImpact:
    """规划和仅看参考的标准整理各自的输入影响范围."""

    planning: tuple[str, ...]
    criteria: tuple[str, ...]
    changed_metadata: tuple[str, ...]


def _changed_anchors(old: ObservationManifest, new: ObservationManifest) -> tuple[ReferenceMeasurement, ...]:
    before = {item.semantic_key(): item for item in old.reference_measurements}
    after = {item.semantic_key(): item for item in new.reference_measurements}
    known = before | after
    return tuple(known[key] for key in before.keys() ^ after.keys())


def observation_impact(
    before: tuple[ObservationManifest, SceneObservations],
    after: tuple[ObservationManifest, SceneObservations],
    revision: ObservationRevisionRequest | None = None,
) -> ObservationImpact:
    """按新旧归属与关系计算影响范围, 没有真实来源绑定的全图变化影响全图."""
    old_manifest, old = before
    new_manifest, new = after
    ids = old.identities | new.identities
    metadata = tuple(name for name in ("context_notes", "gaps", "open_questions") if getattr(old_manifest, name) != getattr(new_manifest, name))
    global_notes = "context_notes" in metadata
    issues = (set(old_manifest.gaps) ^ set(new_manifest.gaps)) | (set(old_manifest.open_questions) ^ set(new_manifest.open_questions))
    changed_refs = {ref for issue in issues for ref in issue.refs}
    unscoped = any(not issue.refs for issue in issues)
    anchors = _changed_anchors(old_manifest, new_manifest)
    if anchors:
        metadata = (*metadata, "reference_measurements")
        if revision is not None and all(
            revision.suggested_region.covers(
                PixelRegion(left=item.region.left, top=item.region.top, right=item.region.right, bottom=item.region.bottom)
            )
            for item in anchors
        ):
            changed_refs.update(revision.disputed_ids)
        else:
            unscoped = True
    planning = set()
    criteria = set()
    for ref in ids:
        previous = probe_observation_dependencies(old, (ref,)) if ref in old.identities else {}
        latest = probe_observation_dependencies(new, (ref,)) if ref in new.identities else {}
        if global_notes or previous != latest:
            criteria.add(ref)
        if ref in criteria or unscoped or changed_refs.intersection(previous.keys() | latest.keys()):
            planning.add(ref)
    if global_notes:
        criteria.add("user_requirement")
    return ObservationImpact(tuple(sorted(planning)), tuple(sorted(criteria)), metadata)


def _protected_choices(sketch: PlanningSketch, changed: set[str]) -> tuple[object, ...]:
    default = {ref: group for ref, group in sketch.default.items() if ref not in changed}
    # 备选可因受影响目标的移除而改变序号. 全场景组合说明仍需通过新方案的引用校验.
    alternatives = ({ref: group for ref, group in option.choices.items() if ref not in changed} for option in sketch.alternatives)
    return (
        default,
        tuple(choices for choices in alternatives if choices),
        tuple(gap for gap in sketch.gaps if changed.isdisjoint(gap.refs)),
        tuple(probe for probe in sketch.risk_probes if changed.isdisjoint(probe.target_ids)),
    )


@dataclass(frozen=True)
class PlanningRevision:
    """已接受旧稿和本轮可改的 E/F/R 来源, 供规划角色就地局部编辑."""

    previous: ScenePlanningSubmission
    affected: tuple[str, ...]
    changed_metadata: tuple[str, ...] = ()

    def validate(self, updated: ScenePlanningSubmission) -> None:
        """拒绝改写任何无关的绑定、备选或其引用的通用机制."""
        changed = set(self.affected)

        def protected_sketches(report: ScenePlanningSubmission) -> dict[str, tuple[object, ...]]:
            rows = {sketch.id: _protected_choices(sketch, changed) for sketch in report.sketches}
            return {key: value for key, value in rows.items() if any(value)}

        def extra(report: ScenePlanningSubmission) -> tuple[object, ...]:
            return tuple(row for row in report.extra_candidates if row.target not in changed)

        if protected_sketches(self.previous) != protected_sketches(updated) or extra(updated) != extra(self.previous):
            msg = "定向修订改变了无关目标的选择、备选、缺口或局部试验。请仅编辑标记的目标"
            raise ValueError(msg)
        bound = {
            m
            for s in self.previous.sketches
            for group in (s.default, *(a.choices for a in s.alternatives))
            for t, methods in group.items()
            if t not in changed
            for m in methods
        }
        bound.update(m for row in self.previous.extra_candidates if row.target not in changed for m in row.mechanisms)
        old = {m.id: m for m in self.previous.mechanisms if m.id in bound}
        new = {m.id: m for m in updated.mechanisms if m.id in bound}
        if old != new:
            msg = "无关目标仍依赖这些机制。如需变化请为受影响目标建立新机制"
            raise ValueError(msg)
