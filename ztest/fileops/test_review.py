from __future__ import annotations

import multiprocessing
import os
from contextlib import nullcontext
from pathlib import Path

from mote.contracts.fileops import ReviewConflictError, ReviewStatus
from mote.runtime.fileops import FileOperations
from mote.runtime.fileops.artifact_budgets import ARTIFACT_WRITE_TTL_SECONDS
from mote.runtime.fileops.artifact_repository import ArtifactWriteScopeState
from mote.runtime.fileops.edit_plans import LiteralEditPlanRequest, WholeFileEditPlanRequest
from mote.runtime.fileops.identity import path_token
from mote.runtime.session.hunk_ops import HunkOps

SESSION = "review-session"


def _operations(root: Path) -> FileOperations:
    return FileOperations(
        session_id=SESSION,
        journal_path=root / "session" / "rollout.jsonl",
        get_project_root=lambda: str(root),
        lock_root=root / "locks",
    )


def _record_delta(operations: FileOperations, **kwargs):
    maximum_bytes = len(kwargs["old"].encode("utf-8")) + len(kwargs["new"].encode("utf-8"))
    scope = operations.artifacts.write_scope(
        owner="test-review-delta",
        maximum_bytes=maximum_bytes,
        ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
    )
    with scope:
        return operations.review.record_delta(scope=scope, **kwargs)


def _artifact_bytes(operations: FileOperations, digest: str) -> bytes:
    return operations.artifacts.read_bytes(operations.artifacts.resolve_live(digest))


def _seed(operations: FileOperations, target: Path, *, old: str, current: str):
    target.write_text(current, encoding="utf-8")
    snapshot, _ = operations.capture(str(target), encoding="utf-8")
    return _record_delta(
        operations,
        path=str(target),
        old=old,
        new=current,
        source="agent",
        turn_index=1,
        tool_call_id="call",
        id_base=f"call:{target.name}",
        expected_digest=snapshot.version.digest,
    )


def _ops(operations: FileOperations) -> HunkOps:
    return HunkOps(
        operations.review,
        operations.artifacts,
        capture_snapshot=operations.capture,
        mutation_factory=operations.mutation_factory,
        commit_mutation_set=operations.mutations.commit,
        resource_lease=operations.mutations.lease,
    )


def _race_review(root_text: str, hunk_id: str, action: str, start, outcomes) -> None:
    operations = _operations(Path(root_text))
    review_ops = _ops(operations)
    start.wait(5)
    result = getattr(review_ops, action)(hunk_id)
    outcomes.put((action, result.ok, result.error))


def _crash_reject(root_text: str, hunk_id: str, stage: str) -> None:
    operations = _operations(Path(root_text))
    review_ops = _ops(operations)
    original_transition = operations.review.transition

    def crashing_transition(record, **kwargs):
        status = kwargs["status"]
        if stage == "after_child_commit" and status == ReviewStatus.REJECTED:
            os._exit(95)
        updated = original_transition(record, **kwargs)
        if stage == "after_rejecting" and status == ReviewStatus.REJECTING:
            os._exit(94)
        return updated

    operations.review.transition = crashing_transition
    review_ops.reject(hunk_id)


def test_review_events_share_rollout_and_survive_new_instance(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    accepted = operations.review.transition(record, status=ReviewStatus.ACCEPTED)

    second = _operations(tmp_path)
    projected = second.review.status(record.hunk_id)
    assert projected == accepted
    assert projected.version == 2
    assert not (tmp_path / "session" / "ledger").exists()
    rollout = (tmp_path / "session" / "rollout.jsonl").read_text(encoding="utf-8")
    assert '"type": "hunk_detected"' in rollout
    assert '"type": "hunk_review_transitioned"' in rollout


def test_committed_file_transaction_projects_prepared_hunks(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("a\n", encoding="utf-8")
    snapshot, _ = operations.capture(str(target), encoding="utf-8")
    operations.observe(snapshot)

    plan = operations.plan_file_edit(
        LiteralEditPlanRequest(
            path=path_token(target),
            old="a",
            new="b",
        )
    )
    result = operations.commit_edit_plan(plan.plan_id, review_turn_index=7).result

    records = operations.review.records()
    assert len(records) == 1
    assert records[0].hunk_id.startswith(result.transaction_id)
    assert records[0].turn_index == 7
    assert records[0].expected_digest == result.versions[0].digest


def test_create_projects_whole_file_insertion_hunk(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "created.txt"

    plan = operations.plan_file_edit(
        WholeFileEditPlanRequest(
            path=path_token(target),
            content="first\nsecond\n",
        )
    )
    result = operations.commit_edit_plan(plan.plan_id, review_turn_index=3).result

    records = operations.review.records()
    assert len(records) == 1
    record = records[0]
    assert record.hunk_id.startswith(result.transaction_id)
    assert record.old_range == (1, 0)
    assert record.new_range == (1, 2)
    assert _artifact_bytes(operations, record.pre_hash) == b""
    assert _artifact_bytes(operations, record.post_hash) == b"first\nsecond\n"
    assert record.expected_digest == result.versions[0].digest


def test_review_transition_is_expected_version_cas(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    operations.review.transition(record, status=ReviewStatus.ACCEPTED)

    try:
        operations.review.transition(record, status=ReviewStatus.REJECTING)
    except ReviewConflictError as exc:
        assert exc.context["expected_version"] == 1
        assert exc.context["actual_version"] == 2
    else:
        raise AssertionError("stale transition unexpectedly succeeded")


def test_accept_and_reject_compete_across_processes(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_race_review,
            args=(str(tmp_path), record.hunk_id, action, start, outcomes),
        )
        for action in ("accept", "reject")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    results = [outcomes.get(timeout=2) for _ in processes]
    assert sum(1 for _, ok, _ in results if ok) == 1, results
    terminal = _operations(tmp_path).review.status(record.hunk_id)
    assert terminal.status in {ReviewStatus.ACCEPTED, ReviewStatus.REJECTED}
    if terminal.status == ReviewStatus.ACCEPTED:
        assert target.read_text(encoding="utf-8") == "b\n"
    else:
        assert target.read_text(encoding="utf-8") == "a\n"


def test_reject_updates_remaining_hunk_version_and_geometry(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    old = "a\nb\nc\nd\ne\nf\n"
    current = "a\nb1\nb2\nc\nd\nE\nf\n"
    upper, lower = _seed(operations, target, old=old, current=current)
    result = _ops(operations).reject(upper.hunk_id)

    assert result.ok
    shifted = operations.review.status(lower.hunk_id)
    assert shifted.status == ReviewStatus.PENDING
    assert shifted.version == 2
    assert shifted.new_range[0] == lower.new_range[0] - 1


def test_reject_post_artifact_scope_completes_after_its_transition_is_durable(
    tmp_path,
    monkeypatch,
):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    original_write_scope = operations.artifacts.write_scope
    review_scopes = []

    def tracked_write_scope(**kwargs):
        scope = original_write_scope(**kwargs)
        if kwargs["owner"] == "hunk-reject-transition":
            review_scopes.append(scope)
            original_complete = scope.complete

            def assert_transition_before_complete(*, durability_root):
                projected = operations.review.status(record.hunk_id)
                assert projected.status == ReviewStatus.REJECTED
                return original_complete(durability_root=durability_root)

            monkeypatch.setattr(scope, "complete", assert_transition_before_complete)
        return scope

    monkeypatch.setattr(operations.artifacts, "write_scope", tracked_write_scope)

    result = _ops(operations).reject(record.hunk_id)

    assert result.ok
    assert len(review_scopes) == 1
    assert review_scopes[0].state == ArtifactWriteScopeState.RELEASED


def test_batch_reject_uses_one_canonical_mutation_set_across_files(tmp_path):
    operations = _operations(tmp_path)
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first_record = _seed(operations, first, old="a\n", current="A\n")[0]
    second_record = _seed(operations, second, old="b\n", current="B\n")[0]
    committed = []
    leased = []

    def commit_mutation_set(mutation_set, ownership):
        committed.append(mutation_set)
        return operations.mutations.commit(mutation_set, ownership)

    def resource_lease(snapshots):
        leased.append(snapshots)
        return nullcontext()

    review_ops = HunkOps(
        operations.review,
        operations.artifacts,
        capture_snapshot=operations.capture,
        mutation_factory=operations.mutation_factory,
        commit_mutation_set=commit_mutation_set,
        resource_lease=resource_lease,
    )

    result = review_ops.reject_all()

    assert result.ok
    assert len(committed) == 1
    mutation_set = committed[0]
    assert tuple(mutation.requested_path.display for mutation in mutation_set.mutations) == (str(first), str(second))
    assert len(leased) == 1
    assert tuple(snapshot.requested_path.display for snapshot in leased[0]) == (
        str(first),
        str(second),
    )
    assert first.read_text(encoding="utf-8") == "a\n"
    assert second.read_text(encoding="utf-8") == "b\n"
    assert {
        operations.review.status(first_record.hunk_id).child_transaction_id,
        operations.review.status(second_record.hunk_id).child_transaction_id,
    } == {mutation_set.transaction_id}


def test_batch_reject_one_file_applies_raw_ranges_high_to_low(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"a\r\nB\nc\rD\r\n")
    snapshot, _ = operations.capture(str(target))
    records = _record_delta(
        operations,
        path=str(target),
        old="a\nb\nc\nd\n",
        new="a\nB\nc\nD\n",
        source="agent",
        turn_index=1,
        id_base="mixed",
        expected_digest=snapshot.version.digest,
    )
    committed = []

    def commit_mutation_set(mutation_set, ownership):
        committed.append(mutation_set)
        return operations.mutations.commit(mutation_set, ownership)

    review_ops = HunkOps(
        operations.review,
        operations.artifacts,
        capture_snapshot=operations.capture,
        mutation_factory=operations.mutation_factory,
        commit_mutation_set=commit_mutation_set,
        resource_lease=operations.mutations.lease,
    )

    result = review_ops.reject_file(str(target))

    assert result.ok
    assert len(records) == 2
    assert len(committed) == 1
    assert len(committed[0].mutations) == 1
    assert target.read_bytes() == b"a\r\nb\r\nc\rd\r\n"


def test_reject_fails_closed_on_external_drift(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    target.write_text("external\n", encoding="utf-8")

    result = _ops(operations).reject(record.hunk_id)

    assert not result.ok
    assert result.error.startswith("drifted")
    assert target.read_text(encoding="utf-8") == "external\n"
    assert operations.review.status(record.hunk_id).status == ReviewStatus.PENDING


def test_rejecting_without_child_transaction_recovers_to_pending(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_reject,
        args=(str(tmp_path), record.hunk_id, "after_rejecting"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 94

    _ops(_operations(tmp_path))

    assert _operations(tmp_path).review.status(record.hunk_id).status == ReviewStatus.PENDING
    assert target.read_text(encoding="utf-8") == "b\n"


def test_committed_child_transaction_finishes_rejected_after_crash(tmp_path):
    operations = _operations(tmp_path)
    target = tmp_path / "target.txt"
    record = _seed(operations, target, old="a\n", current="b\n")[0]
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_reject,
        args=(str(tmp_path), record.hunk_id, "after_child_commit"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 95

    _ops(_operations(tmp_path))

    assert _operations(tmp_path).review.status(record.hunk_id).status == ReviewStatus.REJECTED
    assert target.read_text(encoding="utf-8") == "a\n"
