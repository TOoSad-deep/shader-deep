"""从已接受的 V2 快照发布正式报告或补写交付索引."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue  # noqa: TC002  # Pydantic 在运行时解析凭据内嵌的 JSON 类型.

from shader_deep.domain.review_items import CheckAnswer  # noqa: TC001  # Pydantic 在运行时解析接受快照的答案.
from shader_deep.domain.visual_review import Digest, ReviewModel, ReviewSubmission
from shader_deep.infrastructure.storage.planning_inputs import parse_json
from shader_deep.infrastructure.storage.review_package import (
    MAX_TEXT_BYTES,
    _asset,
    _read_file,
    read_captured_review_source,
    read_review_package,
    write_review_package,
)
from shader_deep.workflows.review.contracts import ReviewOutcome

if sys.platform == "win32":
    import msvcrt
    import time
else:
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Iterator

    from shader_deep.infrastructure.storage.review_package import CapturedReviewSource, ReviewPackage
    from shader_deep.workflows.review.contracts import ReviewResult
    from shader_deep.workflows.review.session import ReviewSession

RECEIPT = "results/publication.json"
ACCEPTED = "results/accepted.json"


def _digest(value: object) -> str:
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def _options(session: ReviewSession) -> dict[str, JsonValue]:
    return {**asdict(session.options), "output_dir": str(session.options.output_dir) if session.options.output_dir is not None else None}


def with_publication_seed(session: ReviewSession, snapshot: dict[str, object]) -> dict[str, object]:
    """把接受时已知的运行事实与答案绑定, 随同一次原子接受保存."""
    execution = {
        **session.execution(),
        "state": "accepted",
        "accepted_snapshot": ACCEPTED,
        "elapsed_seconds": None,
        "elapsed_scope": "acceptance_snapshot",
        "token_usage": None,
        "usage_scope": "unknown_at_acceptance",
        "http_request_count": None,
        "cost": None,
        "cost_status": "unknown",
    }
    seed = {
        "version": "visual_review_v2_acceptance_v1",
        "run_id": session.directory.name,
        "input_sha256": session.source.input_sha256,
        "criteria_sha256": session.source.criteria.criteria_sha256,
        "options": _options(session),
        "execution": execution,
        "accepted_content_sha256": _digest(snapshot),
    }
    return {**snapshot, "publication_seed": seed, "publication_seed_sha256": _digest(seed)}


class _Binding(ReviewModel):
    run_id: str
    input_sha256: Digest
    criteria_sha256: Digest
    options: dict[str, JsonValue]
    execution: dict[str, JsonValue]


class _Seed(_Binding):
    version: Literal["visual_review_v2_acceptance_v1"]
    accepted_content_sha256: Digest


class _Accepted(ReviewModel):
    submission: ReviewSubmission
    check_answers: tuple[CheckAnswer, ...]
    check_revisions: list[JsonValue] | None = None
    publication_seed: _Seed | None = None
    publication_seed_sha256: Digest | None = None


class _Publication(_Binding):
    protocol: Literal["visual_review_v2_publication_v1"] = "visual_review_v2_publication_v1"
    accepted_sha256: Digest
    error: str | None

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class _Receipt(ReviewModel):
    publication: _Publication
    sha256: Digest


def _install_receipt(temporary: Path, destination: Path) -> None:
    """在运行的发布锁内安装新凭据, 兼容不提供硬链接的文件系统."""
    try:
        os.link(temporary, destination)
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM} and getattr(error, "winerror", None) not in {1, 50}:
            raise
    else:
        return
    # 首次发布与重放共用同一把锁. 仅当目标确实不存在时才发布写齐的临时文件.
    try:
        destination.lstat()
    except FileNotFoundError:
        temporary.replace(destination)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)


def _atomic_json(destination: Path, payload: object, *, create: bool = False) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode()
    if not create and destination.is_file() and _read_file(destination, MAX_TEXT_BYTES) == content:
        return
    descriptor, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        if create:
            _install_receipt(temporary, destination)
        else:
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _accepted(directory: Path) -> tuple[_Accepted, str]:
    path = _asset(directory, ACCEPTED)
    if not path.is_file():
        msg = "运行缺少成功接受的 V2 快照, 不能发布"
        raise ValueError(msg)
    content = _read_file(path, MAX_TEXT_BYTES)
    raw = parse_json(content)
    if not isinstance(raw, dict) or "submission" not in raw or "check_answers" not in raw:
        msg = "此运行缺少逐项评审的 V2 接受快照, 不能恢复"
        raise ValueError(msg)
    accepted = _Accepted.model_validate(raw)
    seed = accepted.publication_seed
    if seed is not None:
        body = {key: value for key, value in raw.items() if key not in {"publication_seed", "publication_seed_sha256"}}
        if _digest(raw["publication_seed"]) != accepted.publication_seed_sha256 or _digest(body) != seed.accepted_content_sha256:
            msg = "V2 接受快照中的原始发布绑定或已接受内容不一致"
            raise ValueError(msg)
    elif accepted.publication_seed_sha256 is not None:
        msg = "V2 接受快照缺少原始发布绑定"
        raise ValueError(msg)
    return accepted, hashlib.sha256(content).hexdigest()


def _record(session: ReviewSession, result: ReviewResult) -> _Publication:
    accepted, digest = _accepted(session.directory)
    execution = session.execution()
    if (
        accepted.submission != result.submission
        or accepted.check_answers != session.check_answers
        or accepted.check_revisions != execution.get("check_revisions")
    ):
        msg = "V2 接受快照与本轮已经接受的答案或重评审计不一致"
        raise ValueError(msg)
    return _Publication(
        run_id=session.directory.name,
        input_sha256=session.source.input_sha256,
        criteria_sha256=session.source.criteria.criteria_sha256,
        accepted_sha256=digest,
        execution=execution,
        options=_options(session),
        error=result.error,
    )


@contextmanager
def publication_lock(directory: Path) -> Iterator[None]:
    """串行化原始运行和恢复, 进程退出时由操作系统释放."""
    path = _asset(directory, ".publication.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b"):
        if sys.platform == "win32":
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield


def _save_receipt(directory: Path, publication: _Publication) -> None:
    receipt = _Receipt(publication=publication, sha256=publication.digest())
    _atomic_json(_asset(directory, RECEIPT), receipt.model_dump(mode="json"), create=True)


def _recovery_record(directory: Path, accepted: _Accepted, digest: str) -> _Publication:
    path = _asset(directory, RECEIPT)
    if path.exists():
        receipt = _Receipt.model_validate(parse_json(_read_file(path, MAX_TEXT_BYTES)))
        if receipt.sha256 != receipt.publication.digest() or receipt.publication.run_id != directory.name:
            msg = "发布凭据的内容摘要或原运行身份不一致"
            raise ValueError(msg)
        return receipt.publication
    seed = accepted.publication_seed
    if seed is None or seed.run_id != directory.name:
        msg = "运行缺少与原运行对应的 V2 发布凭据, 不能从旧快照恢复"
        raise ValueError(msg)
    return _Publication(
        run_id=seed.run_id,
        input_sha256=seed.input_sha256,
        criteria_sha256=seed.criteria_sha256,
        accepted_sha256=digest,
        execution=seed.execution,
        options=seed.options,
        error=None,
    )


def _validate_binding(source: CapturedReviewSource, publication: _Publication, accepted: _Accepted, digest: str) -> None:
    execution = publication.execution
    seed = accepted.publication_seed
    if seed is not None and (
        seed.run_id != publication.run_id
        or seed.input_sha256 != publication.input_sha256
        or seed.criteria_sha256 != publication.criteria_sha256
        or seed.options != publication.options
        or seed.execution.get("model_calls") != execution.get("model_calls")
    ):
        msg = "最终发布凭据与原始接受快照绑定不一致"
        raise ValueError(msg)
    if (
        source.request.protocol != "visual_review_v2"
        or execution.get("state") != "accepted"
        or execution.get("accepted_snapshot") != ACCEPTED
        or execution.get("review_protocol") != source.request.protocol
        or digest != publication.accepted_sha256
        or source.input_sha256 != publication.input_sha256
        or source.criteria.criteria_sha256 != publication.criteria_sha256
        or execution.get("input_sha256") != publication.input_sha256
        or execution.get("criteria_sha256") != publication.criteria_sha256
        or accepted.check_revisions != execution.get("check_revisions")
    ):
        msg = "发布凭据与原运行的接受快照、固定输入、标准或重评审计不一致"
        raise ValueError(msg)


def _publish(source: CapturedReviewSource, publication: _Publication, accepted: _Accepted) -> ReviewPackage:
    target = _asset(source.directory, "report")
    if not target.exists():
        write_review_package(target, source, accepted.submission, metadata=publication.execution, check_answers=accepted.check_answers)
    package = read_review_package(target)
    report = package.report
    if (
        report.review_id != publication.run_id
        or report.input_sha256 != publication.input_sha256
        or report.submission != accepted.submission
        or report.check_answers != accepted.check_answers
        or report.metadata != publication.execution
    ):
        msg = "已有正式报告与原运行或接受结果冲突, 拒绝覆盖"
        raise ValueError(msg)
    return package


def _outcome(directory: Path, publication: _Publication, package: ReviewPackage | None, error: str | None) -> ReviewOutcome:
    outcome = ReviewOutcome(
        run_dir=directory,
        report_dir=package.directory if package else None,
        status="completed" if package else "failed",
        verdict=package.report.verdict if package else None,
        error=error,
        execution=publication.execution,
        action_summary=package.report.action_summary if package else None,
    )
    details = {
        "review_protocol": "visual_review_v2",
        "status": outcome.status,
        "verdict": outcome.verdict,
        "error": error,
        "report_path": "report" if package else None,
        "options": publication.options,
        "execution": publication.execution,
    }
    if outcome.action_summary is not None:
        details["action_summary"] = outcome.action_summary.model_dump(mode="json")
    manifest, result = _asset(directory, "manifest.json"), _asset(directory, "result.json")
    _atomic_json(manifest, publication.execution)
    _atomic_json(result, details)
    return outcome


def deliver_accepted_review(session: ReviewSession, result: ReviewResult) -> ReviewOutcome:
    """在首次正式发布前登记可供离线重放的 V2 接受事实."""
    publication = _record(session, result)
    try:
        _save_receipt(session.directory, publication)
        accepted, digest = _accepted(session.directory)
        _validate_binding(session.source, publication, accepted, digest)
        package = _publish(session.source, publication, accepted)
    except Exception as error:  # noqa: BLE001  # 发布异常保留接受快照与原始诊断, 供离线重试.
        reason = f"publish_error: {type(error).__name__}: {error}"
        return _outcome(session.directory, publication, None, f"{result.error}; {reason}" if result.error else reason)
    return _outcome(session.directory, publication, package, result.error)


def republish_review(run_dir: Path) -> ReviewOutcome:
    """从 V2 运行的已接受快照发布报告或恢复交付索引.

    Args:
        run_dir: 原运行目录, 与接受时的运行目录同名.

    Returns:
        已核验报告的位置、视觉结论及原始执行信息.

    Raises:
        ValueError: 缺少发布凭据、绑定不匹配或已有报告冲突.
        OSError: 读取、发布报告或更新运行索引失败.
    """
    if run_dir.is_symlink():
        msg = "评审运行目录不能为符号链接"
        raise ValueError(msg)
    directory = run_dir.resolve(strict=True)
    if not _asset(directory, ACCEPTED).is_file():
        msg = "运行缺少成功接受的 V2 快照, 不能发布"
        raise ValueError(msg)
    with publication_lock(directory):
        accepted, digest = _accepted(directory)
        publication = _recovery_record(directory, accepted, digest)
        source = read_captured_review_source(directory)
        _validate_binding(source, publication, accepted, digest)
        # 先验证写入位置, 索引含符号链接时也不产生新的正式包.
        _asset(directory, "manifest.json")
        _asset(directory, "result.json")
        package = _publish(source, publication, accepted)
        if not _asset(directory, RECEIPT).exists():
            _save_receipt(directory, publication)
        return _outcome(directory, publication, package, publication.error)
