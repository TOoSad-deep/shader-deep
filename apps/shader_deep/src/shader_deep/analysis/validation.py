"""在黑板登记边界校验分析任务归属及跨报告的证据引用."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shader_deep.analysis.exploration import ExplorationReport, PossibilityLibrary, validate_exploration, validate_library
from shader_deep.analysis.references import report_catalog
from shader_deep.analysis.schemas import AnalysisSummary, AnalysisValidationError as AnalysisValidationError, LensReport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shader_deep.analysis.evidence import ImageRegion
    from shader_deep.analysis.schemas import (
        AnalysisTaskRequest,
        ImplementationSketch,
        SourceKind,
        SourceRef,
        VisualDecomposition,
        VisualElement,
        VisualMapping,
    )
    from shader_deep.schemas import BlackboardState, ResultRecord, TaskRecord


# 所有来源限制共用这里的类型集合, 目录只枚举真实产出而不改变各字段的权限.
REFERENCE_KINDS: dict[str, frozenset[SourceKind]] = {
    "observation": frozenset({"observation"}),
    "statement": frozenset({"report", "observation", "interpretation", "visual_element", "visual_feature", "visual_relation"}),
    "derived_from": frozenset({"interpretation"}),
}


def _issue(path: str, code: str, message: str) -> dict[str, object]:
    """统一业务错误的最小结构, 不将错误记录变为另一套校验框架."""
    return {"path": path, "code": code, "message": message}


def _source_issue(result: ResultRecord, reference: SourceRef, path: str, rule: str) -> dict[str, object] | None:
    """从真实目录核对身份与可选声明类型, 给出同报告中的少量合法候选."""
    entries = report_catalog(result)
    actual = next((entry for entry in entries if entry["item_id"] == reference.item_id), None)
    allowed = REFERENCE_KINDS[rule]
    if actual and actual["kind"] in allowed and (reference.kind is None or reference.kind == actual["kind"]):
        return None
    label = "observation" if rule == "observation" else "item"
    message = f"Invalid {label} reference: {reference.result_id}/{reference.item_id}; allowed={sorted(allowed)}"
    if actual:
        message += f"; actual={actual['kind']}; declared={reference.kind}"
    issue = _issue(path, "source_kind_mismatch" if actual else "source_not_found", message)
    issue["candidates"] = [entry for entry in entries if entry["kind"] in allowed][:5]
    return issue


def validate_evidence(
    state: BlackboardState, task: TaskRecord, identifiers: tuple[str, ...], *, measured: bool = False, path: str = "/evidence_ids"
) -> None:
    """校验证据归属、任务可见范围及数值依据要求.

    Args:
        state: 保存程序生成的测量记录的黑板.
        task: 正在选择或引用证据的任务.
        identifiers: 明确指定的证据 ID.
        measured: 当前结论是否声明受到数值测量支持.
        path: 完整参数中证据列表的路径.
    """
    issues = _evidence_issues(state, task, identifiers, measured=measured, path=path)
    if issues:
        raise AnalysisValidationError(issues)


def _evidence_issues(state: BlackboardState, task: TaskRecord, identifiers: tuple[str, ...], *, measured: bool, path: str) -> list[dict[str, object]]:
    """先独立检查每条证据归属, 全部合法后才判断是否有数值依据."""
    records = state.get("measurements", {})
    parent = task.parent_task_id or task.id
    issues: list[dict[str, object]] = []
    for index, identifier in enumerate(identifiers):
        item = records.get(identifier)
        if item is None or item.parent_task_id != parent or item.target_version != task.target_version:
            msg = f"Evidence is outside analysis: {identifier}"
            issues.append(_issue(f"{path}/{index}", "evidence_outside_analysis", msg))
        elif task.lens_config is not None and identifier not in task.evidence_ids:
            msg = f"Evidence was not provided to this task: {identifier}"
            issues.append(_issue(f"{path}/{index}", "evidence_not_provided", msg))
    # 裁剪虽由程序生成, 但只增加可查看的图像材料; 声明数值支持必须引用统计或剖面.
    if not issues and measured and not any(records[identifier].spec.kind != "crop" for identifier in identifiers):
        msg = "measurement_supported requires a numeric measurement reference; a crop is visual evidence"
        issues.append(_issue(path, "numeric_evidence_required", msg))
    return issues


def validate_analysis_task(state: BlackboardState, task: TaskRecord) -> None:
    """校验视角子任务是否归属于同一目标下的主分析任务.

    Args:
        state: 包含父任务与明确选入的历史结果的黑板.
        task: 准备登记的任务记录.
    """
    if task.parent_task_id is None and task.lens_config is None:
        if task.evidence_ids:
            msg = "Root tasks receive measurements through their analysis session"
            raise AnalysisValidationError([_issue("/evidence_ids", "root_evidence_binding", msg)])
        return
    parent = state["tasks"].get(task.parent_task_id or "")
    if (
        task.role != "analysis"
        or task.lens_config is None
        or parent is None
        or parent.role != "analysis"
        or parent.lens_config is not None
        or parent.target_version != task.target_version
    ):
        msg = "Lens tasks require a root analysis parent with the same target"
        raise ValueError(msg)
    issues = _evidence_issues(state, task, task.evidence_ids, measured=False, path="/evidence_ids")
    for index, identifier in enumerate(task.related_result_ids):
        result = state["results"][identifier]
        source = state["tasks"][result.task_id]
        if source.parent_task_id != parent.id or not isinstance(result.analysis_detail, LensReport):
            msg = f"Analysis input is outside parent task: {identifier}"
            issues.append(_issue(f"/related_result_ids/{index}", "source_outside_analysis", msg))
    if issues:
        raise AnalysisValidationError(issues)


def _summary_source_issues(
    state: BlackboardState, task: TaskRecord, detail: AnalysisSummary, references: tuple[SourceRef, ...], path: str, rule: str
) -> list[dict[str, object]]:
    """收集独立来源错误; 来源报告无效时不继续推断其条目内容."""
    issues: list[dict[str, object]] = []
    for index, reference in enumerate(references):
        location = f"{path}/{index}"
        if reference.result_id not in detail.source_result_ids:
            issues.append(_issue(location, "undeclared_source", f"Undeclared source result: {reference.result_id}"))
            continue
        result = state["results"].get(reference.result_id)
        source = state["tasks"].get(result.task_id) if result else None
        if result and source and source.parent_task_id == task.id and source.target_version == task.target_version:
            issue = _source_issue(result, reference, location, rule)
            if issue:
                issues.append(issue)
    return issues


def _hypothesis_link_issues(state: BlackboardState, task: TaskRecord, detail: AnalysisSummary) -> list[dict[str, object]]:
    """继承目标属于综合假设, 来源只允许具体解释, 不要求覆盖全部解释."""
    hypotheses = {item.id for item in detail.hypotheses if item.id is not None}
    issues: list[dict[str, object]] = []
    for index, link in enumerate(detail.hypothesis_links):
        path = f"/summary/hypothesis_links/{index}"
        if link.hypothesis_id not in hypotheses:
            issues.append(_issue(f"{path}/hypothesis_id", "hypothesis_not_found", f"Unknown summary hypothesis: {link.hypothesis_id}"))
        issues.extend(_summary_source_issues(state, task, detail, link.derived_from, f"{path}/derived_from", "derived_from"))
    return issues


def _validate_summary(state: BlackboardState, task: TaskRecord, detail: AnalysisSummary) -> None:
    """逐层核对来源报告、陈述条目、数值证据和缺失任务的归属."""
    if not detail.source_result_ids:
        msg = "Summary source IDs must be nonempty and unique"
        raise AnalysisValidationError([_issue("/summary/source_result_ids", "invalid_source_ids", msg)])
    issues = _summary_report_issues(state, task, detail)
    if issues:
        raise AnalysisValidationError(issues)
    for group in ("key_observations", "relationships", "hypotheses", "disagreements", "open_questions", "implementation_hints"):
        for index, statement in enumerate(getattr(detail, group)):
            path = f"/summary/{group}/{index}"
            rule = "observation" if group == "key_observations" else "statement"
            issues.extend(_summary_source_issues(state, task, detail, statement.source_refs, f"{path}/source_refs", rule))
            issues.extend(
                _evidence_issues(
                    state, task, statement.evidence_ids, measured=statement.basis == "measurement_supported", path=f"{path}/evidence_ids"
                )
            )
    issues.extend(_hypothesis_link_issues(state, task, detail))
    issues.extend(_visual_evidence_issues(state, task, detail.visual_decomposition, "/summary/visual_decomposition"))
    for index, identifier in enumerate(detail.missing_task_ids):
        missing = state["tasks"].get(identifier)
        if missing is None or missing.parent_task_id != task.id:
            msg = f"Missing task is outside analysis: {identifier}"
            issues.append(_issue(f"/summary/missing_task_ids/{index}", "task_outside_analysis", msg))
    if issues:
        raise AnalysisValidationError(issues)


def _summary_report_issues(state: BlackboardState, task: TaskRecord, detail: AnalysisSummary) -> list[dict[str, object]]:
    """先核对来源身份与归属, 后续条目错误只针对可解析报告收集."""
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, identifier in enumerate(detail.source_result_ids):
        if identifier in seen:
            issues.append(_issue(f"/summary/source_result_ids/{index}", "duplicate_source_id", "Summary source IDs must be nonempty and unique"))
        seen.add(identifier)
        result = state["results"].get(identifier)
        source = state["tasks"].get(result.task_id) if result else None
        if (
            result is None
            or source is None
            or source.parent_task_id != task.id
            or source.target_version != task.target_version
            or not isinstance(result.analysis_detail, LensReport)
        ):
            issues.append(_issue(f"/summary/source_result_ids/{index}", "invalid_source_report", f"Summary source is outside analysis: {identifier}"))
    return issues


def _validate_new_analysis(detail: ExplorationReport | PossibilityLibrary, task: TaskRecord) -> None:
    if isinstance(detail, ExplorationReport):
        if task.lens_config is None or task.parent_task_id is None or task.analysis_outline is None:
            msg = "Exploration reports require an independent task with its fixed visual outline"
            raise ValueError(msg)
        if task.related_result_ids or task.evidence_ids:
            msg = "Exploration tasks cannot inherit reports or measurements"
            raise ValueError(msg)
        validate_exploration(detail, task.analysis_outline)
    else:
        if task.lens_config is not None or task.parent_task_id is not None:
            msg = "Possibility libraries require a root analysis task"
            raise ValueError(msg)
        validate_library(detail)


def validate_analysis_result(state: BlackboardState, result: ResultRecord, task: TaskRecord) -> None:
    """校验报告归属, 以及综合陈述的来源链路.

    Args:
        state: 包含全部被引用记录的黑板.
        result: 准备登记的结果, 可附带结构化分析内容.
        task: 该结果的来源任务.
    """
    detail = result.analysis_detail
    if detail is None:
        return
    # 结构化分析内容只写入 analysis_detail, 避免同一结论在通用字段中另存一份而相互矛盾.
    if task.role != "analysis" or result.observations or result.hypotheses or result.limitations or result.recommendation:
        msg = "Analysis content belongs only in analysis_detail on an analysis task"
        raise ValueError(msg)
    if isinstance(detail, (ExplorationReport, PossibilityLibrary)):
        _validate_new_analysis(detail, task)
    elif isinstance(detail, LensReport):
        if task.lens_config is None or task.parent_task_id is None:
            msg = "Lens reports require a lens task"
            raise ValueError(msg)
        issues = _visual_evidence_issues(state, task, detail.visual_additions, "/report/visual_additions")
        for index, observation in enumerate(detail.observations):
            issues.extend(
                _evidence_issues(
                    state,
                    task,
                    observation.evidence_ids,
                    measured=observation.basis == "measurement_supported",
                    path=f"/report/observations/{index}/evidence_ids",
                )
            )
        if issues:
            raise AnalysisValidationError(issues)
    elif isinstance(detail, AnalysisSummary) and task.lens_config is None and task.parent_task_id is None:
        _validate_summary(state, task, detail)
    else:
        msg = "Analysis summaries require a root analysis task"
        raise ValueError(msg)


def _visual_ids(decomposition: VisualDecomposition | None) -> dict[str, set[str]]:
    """保留对象类别, 避免特征 ID 被误用为元素引用."""
    return {
        "element": {item.id for item in decomposition.elements} if decomposition else set(),
        "feature": {item.id for item in decomposition.features} if decomposition else set(),
        "relation": {item.id for item in decomposition.relations} if decomposition else set(),
    }


def _reference_issues(identifiers: tuple[str, ...], available: set[str], label: str, path: str) -> list[dict[str, object]]:
    """列出不存在或重复的引用, 每条定位到原列表位置."""
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, identifier in enumerate(identifiers):
        if identifier not in available or identifier in seen:
            msg = f"Invalid {label} references: {identifiers}; available={sorted(available)}"
            issues.append(_issue(f"{path}/{index}", "invalid_reference", msg))
        seen.add(identifier)
    return issues


def _visual_identity_issues(decomposition: VisualDecomposition, base: VisualDecomposition | None, path: str) -> list[dict[str, object]]:
    """先检查当前视觉条目的身份, 固定初稿只贡献已占用 ID."""
    seen = set().union(*_visual_ids(base).values())
    issues: list[dict[str, object]] = []
    for group in ("elements", "features", "relations"):
        for index, item in enumerate(getattr(decomposition, group)):
            if item.id in seen:
                msg = "Visual object IDs must be unique, including the bound draft"
                issues.append(_issue(f"{path}/{group}/{index}/id", "duplicate_visual_id", msg))
            seen.add(item.id)
    return issues


def validate_visual_decomposition(
    decomposition: VisualDecomposition | None,
    *,
    base: VisualDecomposition | None = None,
    image_size: tuple[int, int] | None = None,
    path: str = "/visual_decomposition",
) -> None:
    """校验视觉对象标识、关联及父子无环约束.

    Args:
        decomposition: 完整视觉结构或报告中的局部新增对象.
        base: 局部新增对象可引用的固定初稿; 不允许覆盖其 ID.
        image_size: 已固定的原图尺寸, 提供时检查像素区域上界.
        path: 完整参数中当前视觉结构的路径.
    """
    if base is not None:
        try:
            validate_visual_decomposition(base, image_size=image_size)
        except AnalysisValidationError as exc:
            # 初稿属于固定输入而非当前提交, 不将它的路径伪装为本次可修复字段.
            msg = f"Bound visual decomposition is invalid: {exc}"
            raise ValueError(msg) from exc
    if decomposition is None:
        return
    # 当前参数只新增或替换自己的结构, 不提供修改固定初稿的路径.
    issues = _visual_identity_issues(decomposition, base, path)
    if issues:
        raise AnalysisValidationError(issues)
    elements = {item.id: item for structure in (base, decomposition) if structure for item in structure.elements}
    for group in ("features", "relations"):
        for index, item in enumerate(getattr(decomposition, group)):
            issues.extend(_reference_issues(item.element_ids, set(elements), "visual element", f"{path}/{group}/{index}/element_ids"))
    paths = {item.id: f"{path}/elements/{index}/parent_id" for index, item in enumerate(decomposition.elements)}
    issues.extend(_parent_issues(elements, paths))
    for group in ("elements", "features"):
        for index, item in enumerate(getattr(decomposition, group)):
            issues.extend(_region_issues(item.region_box, image_size, f"{path}/{group}/{index}/region_box"))
    if issues:
        raise AnalysisValidationError(issues)


def _region_issues(region: ImageRegion | None, image_size: tuple[int, int] | None, path: str) -> list[dict[str, object]]:
    """像素区域已检查正面积, 此处只核对实际图像边界."""
    if region and image_size and (region.right > image_size[0] or region.bottom > image_size[1]):
        msg = f"Visual region is outside image: {region}; image_size={image_size}"
        return [_issue(path, "region_outside_image", msg)]
    return []


def _parent_issues(elements: dict[str, VisualElement], paths: dict[str, str]) -> list[dict[str, object]]:
    """沿父链检查缺失对象和环, 保留原始树形命名."""
    issues: list[dict[str, object]] = []
    for identifier, path in paths.items():
        parent = elements[identifier].parent_id
        if parent is not None and parent not in elements:
            issues.append(_issue(path, "invalid_parent", f"Visual parent cycle or missing element: {parent}"))
    if issues:
        return issues
    for identifier, path in paths.items():
        visited: set[str] = set()
        current: str | None = identifier
        while current is not None:
            if current in visited or current not in elements:
                msg = f"Visual parent cycle or missing element: {current}"
                # 环外后代无需修改父边; 环上各条父边会从对应元素开始被定位.
                if current == identifier or current not in elements:
                    issues.append(_issue(path, "invalid_parent", msg))
                break
            visited.add(current)
            current = elements[current].parent_id
    return issues


def validate_task_visual(request: AnalysisTaskRequest, decomposition: VisualDecomposition | None, *, path: str = "") -> None:
    """核对任务关注范围是否属于将要绑定的视觉结构.

    Args:
        request: 尚未登记的任务请求.
        decomposition: 该批次绑定的不可变结构.
        path: 完整参数中任务请求的路径.
    """
    identifiers = _visual_ids(decomposition)
    issues = _reference_issues(request.focus_element_ids, identifiers["element"], "focus element", f"{path}/focus_element_ids")
    issues.extend(_reference_issues(request.focus_feature_ids, identifiers["feature"], "focus feature", f"{path}/focus_feature_ids"))
    if issues:
        raise AnalysisValidationError(issues)


def validate_visual_evidence(
    state: BlackboardState, task: TaskRecord, decomposition: VisualDecomposition | None, *, path: str = "/visual_decomposition"
) -> None:
    """校验视觉条目的真实测量与报告来源, 未列来源的条目保持未独立核对状态.

    Args:
        state: 当前分析黑板.
        task: 选择结构或提交条目的任务.
        decomposition: 待检查的结构; 缺失表示未提供.
        path: 工具参数中的结构路径, 用于局部修复反馈.
    """
    issues = _visual_evidence_issues(state, task, decomposition, path)
    if issues:
        raise AnalysisValidationError(issues)


def _visual_evidence_issues(
    state: BlackboardState, task: TaskRecord, decomposition: VisualDecomposition | None, path: str
) -> list[dict[str, object]]:
    """独立检查每个视觉条目的来源与测量, 保留精确的数组位置."""
    issues: list[dict[str, object]] = []
    if decomposition is None:
        return issues
    for group in ("elements", "features", "relations"):
        for index, item in enumerate(getattr(decomposition, group)):
            location = f"{path}/{group}/{index}"
            issues.extend(
                _evidence_issues(state, task, item.evidence_ids, measured=item.basis == "measurement_supported", path=f"{location}/evidence_ids")
            )
            for number, reference in enumerate(item.source_refs):
                issue = _visual_source_issue(state, task, reference, f"{location}/source_refs/{number}")
                if issue:
                    issues.append(issue)
    return issues


def _visual_source_issue(state: BlackboardState, task: TaskRecord, reference: SourceRef, path: str) -> dict[str, object] | None:
    """视觉观察只能引用同分析内的观察, 不将机制解释升级为事实."""
    result = state["results"].get(reference.result_id)
    source = state["tasks"].get(result.task_id) if result else None
    if result is None or source is None or source.parent_task_id != (task.parent_task_id or task.id) or source.target_version != task.target_version:
        return _issue(path, "source_outside_analysis", f"Visual source is outside analysis: {reference.result_id}")
    if task.lens_config is not None and reference.result_id not in task.related_result_ids:
        return _issue(path, "source_not_provided", f"Visual source was not provided to this task: {reference.result_id}")
    issue = _source_issue(result, reference, path, "observation")
    if issue:
        issue["message"] = f"Visual source must reference an observation: {reference.result_id}/{reference.item_id}; {issue['message']}"
    return issue


def _scope_issues(element_ids: tuple[str, ...], feature_ids: tuple[str, ...], identifiers: dict[str, set[str]], path: str) -> list[dict[str, object]]:
    """分别解析元素与特征范围."""
    return [
        *_reference_issues(element_ids, identifiers["element"], "element", f"{path}/element_ids"),
        *_reference_issues(feature_ids, identifiers["feature"], "feature", f"{path}/feature_ids"),
    ]


def _sketch_issues(
    sketches: tuple[ImplementationSketch, ...], hypotheses: set[str], identifiers: dict[str, set[str]], image_size: tuple[int, int] | None, path: str
) -> list[dict[str, object]]:
    """确保每套实现方案关联明确假设, 并保留方案间独立身份."""
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, sketch in enumerate(sketches):
        if sketch.id in seen:
            issues.append(_issue(f"{path}/{index}/id", "duplicate_sketch_id", "Implementation sketch IDs must be unique"))
        seen.add(sketch.id)
    if issues:
        return issues
    for index, sketch in enumerate(sketches):
        location = f"{path}/{index}"
        issues.extend(_reference_issues(sketch.hypothesis_ids, hypotheses, "hypothesis", f"{location}/hypothesis_ids"))
        issues.extend(_scope_issues(sketch.element_ids, sketch.feature_ids, identifiers, location))
        for number, checkpoint in enumerate(sketch.render_checkpoints):
            issues.extend(_region_issues(checkpoint.region_box, image_size, f"{location}/render_checkpoints/{number}/region_box"))
    return issues


def validate_lens_visual(report: LensReport, decomposition: VisualDecomposition | None = None, *, image_size: tuple[int, int] | None = None) -> None:
    """使用任务冻结初稿核对子报告新增结构、局部修订和逐假设方案.

    Args:
        report: 待提交的原始子报告.
        decomposition: 该任务已实际接收的视觉初稿.
        image_size: 已固定的原图尺寸.
    """
    validate_visual_decomposition(report.visual_additions, base=decomposition, image_size=image_size, path="/report/visual_additions")
    additions = _visual_ids(report.visual_additions)
    collisions = set().union(*additions.values()) & {item.id for item in (*report.observations, *report.interpretations)}
    if collisions and report.visual_additions:
        msg = f"Visual addition IDs collide with report item IDs: {sorted(collisions)}"
        issues = [
            _issue(f"/report/visual_additions/{group}/{index}/id", "report_item_collision", msg)
            for group in ("elements", "features", "relations")
            for index, item in enumerate(getattr(report.visual_additions, group))
            if item.id in collisions
        ]
        raise AnalysisValidationError(issues)
    identifiers = _visual_ids(decomposition)
    for kind, values in additions.items():
        identifiers[kind].update(values)
    issues: list[dict[str, object]] = []
    for group in ("observations", "interpretations"):
        for index, item in enumerate(getattr(report, group)):
            issues.extend(_scope_issues(item.element_ids, item.feature_ids, identifiers, f"/report/{group}/{index}"))
    for index, observation in enumerate(report.observations):
        issues.extend(_region_issues(observation.region_box, image_size, f"/report/observations/{index}/region_box"))
    for index, revision in enumerate(report.visual_revisions):
        issues.extend(
            _reference_issues(
                revision.target_ids, set().union(*identifiers.values()), "revision target", f"/report/visual_revisions/{index}/target_ids"
            )
        )
    issues.extend(
        _sketch_issues(
            report.implementation_sketches, {item.id for item in report.interpretations}, identifiers, image_size, "/report/implementation_sketches"
        )
    )
    if issues:
        raise AnalysisValidationError(issues)


def validate_summary_visual(
    state: BlackboardState,
    summary: AnalysisSummary,
    *,
    decomposition: VisualDecomposition | None = None,
    source_decompositions: Mapping[str, VisualDecomposition | None] | None = None,
    image_size: tuple[int, int] | None = None,
) -> None:
    """核对综合对象、原始来源映射与逐假设方案, 不判断自然语言假设真假.

    Args:
        state: 包含原始子报告的当前黑板.
        summary: 待提交综合报告.
        decomposition: 首次保存的视觉初稿; 后续修订保存在各任务快照中.
        source_decompositions: 每份子报告实际接收的固定初稿, 以结果 ID 为键.
        image_size: 已固定的原图尺寸.
    """
    validate_visual_decomposition(summary.visual_decomposition, image_size=image_size, path="/summary/visual_decomposition")
    identifiers = _visual_ids(summary.visual_decomposition)
    hypotheses = [item.id for item in summary.hypotheses if item.id is not None]
    issues: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, item in enumerate(summary.hypotheses):
        if item.id is not None:
            if item.id in seen:
                issues.append(_issue(f"/summary/hypotheses/{index}/id", "duplicate_hypothesis_id", "Summary hypothesis IDs must be unique"))
            seen.add(item.id)
    if issues:
        raise AnalysisValidationError(issues)
    for group in ("key_observations", "relationships", "hypotheses", "disagreements", "open_questions", "implementation_hints"):
        for index, item in enumerate(getattr(summary, group)):
            issues.extend(_scope_issues(item.element_ids, item.feature_ids, identifiers, f"/summary/{group}/{index}"))
    issues.extend(_sketch_issues(summary.implementation_sketches, set(hypotheses), identifiers, image_size, "/summary/implementation_sketches"))
    issues.extend(_summary_visual_source_issues(summary))
    issues.extend(_summary_mapping_issues(state, summary, decomposition, source_decompositions or {}))
    if issues:
        raise AnalysisValidationError(issues)


def _summary_visual_source_issues(summary: AnalysisSummary) -> list[dict[str, object]]:
    """核对视觉条目是否只引用综合报告声明的来源."""
    issues: list[dict[str, object]] = []
    if summary.visual_decomposition:
        for group in ("elements", "features", "relations"):
            for index, item in enumerate(getattr(summary.visual_decomposition, group)):
                for number, source in enumerate(item.source_refs):
                    if source.result_id not in summary.source_result_ids:
                        path = f"/summary/visual_decomposition/{group}/{index}/source_refs/{number}/result_id"
                        issues.append(_issue(path, "undeclared_source", f"Invalid visual source result references: {source.result_id}"))
    return issues


def _summary_mapping_issues(
    state: BlackboardState, summary: AnalysisSummary, decomposition: VisualDecomposition | None, snapshots: Mapping[str, VisualDecomposition | None]
) -> list[dict[str, object]]:
    """映射来源和目标分别检查, 一个方向错误不掩盖另一方向的可修复字段."""
    identifiers = _visual_ids(summary.visual_decomposition)
    errors: list[dict[str, object]] = []
    for index, mapping in enumerate(summary.visual_mappings):
        # 来源无效也继续检查目标, 一次返回所有映射问题以减少整份报告重交.
        try:
            original = _mapping_source(state, summary, mapping, decomposition, snapshots)
        except ValueError as exc:
            errors.append(_issue(f"/summary/visual_mappings/{index}/source_result_id", "invalid_mapping_source", f"visual_mappings[{index}]: {exc}"))
        else:
            if mapping.source_id not in original[mapping.kind]:
                msg = (
                    f"visual_mappings[{index}]: Invalid mapping source references: {(mapping.source_id,)}; available={sorted(original[mapping.kind])}"
                )
                errors.append(_issue(f"/summary/visual_mappings/{index}/source_id", "invalid_mapping_source", msg))
        if mapping.target_id not in identifiers[mapping.kind]:
            msg = (
                f"visual_mappings[{index}]: Invalid mapping target references: {(mapping.target_id,)}; available={sorted(identifiers[mapping.kind])}"
            )
            errors.append(_issue(f"/summary/visual_mappings/{index}/target_id", "invalid_mapping_target", msg))
    return errors


def _mapping_source(
    state: BlackboardState,
    summary: AnalysisSummary,
    mapping: VisualMapping,
    decomposition: VisualDecomposition | None,
    snapshots: Mapping[str, VisualDecomposition | None],
) -> dict[str, set[str]]:
    """报告局部编号只在报告命名空间内解析, 不让其他报告代替来源."""
    if mapping.source_result_id is None:
        return _visual_ids(decomposition)
    result = state["results"].get(mapping.source_result_id)
    if mapping.source_result_id not in summary.source_result_ids or result is None or not isinstance(result.analysis_detail, LensReport):
        msg = f"Undeclared visual mapping source: {mapping.source_result_id}"
        raise ValueError(msg)
    identifiers = _visual_ids(snapshots.get(mapping.source_result_id))
    for kind, values in _visual_ids(result.analysis_detail.visual_additions).items():
        identifiers[kind].update(values)
    return identifiers
