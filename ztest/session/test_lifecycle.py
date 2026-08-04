from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.artifact import ArtifactOwnerKind
from mote.contracts.runtime.errors import LeaseFencedError, LeaseUnavailableError
from mote.contracts.session import (
    SessionBlockerKind,
    SessionDeletionClaim,
    SessionDeletionCommand,
    SessionDeletionState,
    SessionId,
    SessionLifecycleState,
)
from mote.runtime.artifacts.store import DurableArtifactStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.session.deletion import SessionDeletionExecutor
from mote.runtime.session.lifecycle import SessionLifecycleConflictError, SessionLifecycleStore
from mote.runtime.session.stream_ownership import SessionStreamOwnership


def test_session_deletion_requires_terminal_closed_retention_and_fences_every_stage(tmp_path):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    clock = [now]
    store = SessionLifecycleStore(tmp_path / "lifecycle.sqlite3", clock=lambda: clock[0])
    session_id = SessionId("session-a")
    active = store.activate(session_id)
    blocked = store.replace_blockers(
        session_id,
        (SessionBlockerKind.WORKFLOW,),
        expected_generation=active.lifecycle_generation,
        expected_revision=active.revision,
    )
    draining = store.set_state(
        session_id,
        SessionLifecycleState.DRAINING,
        expected_generation=blocked.lifecycle_generation,
        expected_revision=blocked.revision,
    )
    with pytest.raises(SessionLifecycleConflictError):
        store.set_state(
            session_id,
            SessionLifecycleState.TERMINAL,
            expected_generation=draining.lifecycle_generation,
            expected_revision=draining.revision,
        )
    closed = store.replace_blockers(
        session_id,
        (),
        expected_generation=draining.lifecycle_generation,
        expected_revision=draining.revision,
    )
    terminal = store.set_state(
        session_id,
        SessionLifecycleState.TERMINAL,
        expected_generation=closed.lifecycle_generation,
        expected_revision=closed.revision,
    )
    command = SessionDeletionCommand(
        "delete-a", session_id, "operator", now, terminal.lifecycle_generation, terminal.revision
    )
    with pytest.raises(SessionLifecycleConflictError):
        store.claim_deletion(command, owner_id="maintenance", fencing_token=1)

    clock[0] += timedelta(days=31)
    command = SessionDeletionCommand(
        "delete-a", session_id, "operator", clock[0], terminal.lifecycle_generation, terminal.revision
    )
    claim = store.claim_deletion(command, owner_id="maintenance", fencing_token=1)
    receipt = store.advance_deletion(claim, SessionDeletionState.REFERENCES_RELEASING)
    with pytest.raises(SessionLifecycleConflictError):
        store.advance_deletion(claim, SessionDeletionState.METADATA_TOMBSTONED)
    claim = SessionDeletionClaim(
        claim.command_id,
        claim.session_id,
        claim.lifecycle_generation,
        receipt.revision,
        claim.owner_id,
        claim.fencing_token,
    )
    for state in (
        SessionDeletionState.METADATA_TOMBSTONED,
        SessionDeletionState.BLOBS_RECLAIMING,
        SessionDeletionState.DIRECTORY_RETIRING,
        SessionDeletionState.SETTLED,
    ):
        receipt = store.advance_deletion(claim, state)
        claim = SessionDeletionClaim(
            claim.command_id,
            claim.session_id,
            claim.lifecycle_generation,
            receipt.revision,
            claim.owner_id,
            claim.fencing_token,
        )
    assert store.get(session_id).state is SessionLifecycleState.TOMBSTONED


def test_session_retention_scan_is_bounded_and_excludes_active_or_blocked(tmp_path):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = SessionLifecycleStore(tmp_path / "lifecycle.sqlite3", clock=lambda: now)
    for value in ("a", "b"):
        active = store.activate(SessionId(value))
        draining = store.set_state(
            SessionId(value),
            SessionLifecycleState.DRAINING,
            expected_generation=active.lifecycle_generation,
            expected_revision=active.revision,
        )
        store.set_state(
            SessionId(value),
            SessionLifecycleState.TERMINAL,
            expected_generation=draining.lifecycle_generation,
            expected_revision=draining.revision,
        )
    assert tuple(item.session_id for item in store.scan_retention_eligible(before=now, limit=1)) == ("a",)
    assert tuple(
        item.session_id for item in store.scan_retention_eligible(before=now, after_session_id="a", limit=1)
    ) == ("b",)


def test_session_executor_releases_only_its_edges_and_retires_exact_directory(tmp_path):
    class Blobs:
        def write_bytes(self, content):
            raise AssertionError("not used")

        def read_bytes(self, ref):
            raise AssertionError("not used")

        def delete(self, ref):
            return False

    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    clock = [now]
    lifecycle = SessionLifecycleStore(tmp_path / "lifecycle.sqlite3", clock=lambda: clock[0])
    artifacts = DurableArtifactStore(tmp_path / "artifacts.sqlite3", Blobs())
    session_id = SessionId("owned")
    active = lifecycle.activate(session_id)
    artifacts.replace_ownership_edges(
        owner_kind=ArtifactOwnerKind.SESSION,
        owner_id=str(session_id),
        generation=active.lifecycle_generation,
        content_digests=("a" * 64,),
    )
    artifacts.replace_ownership_edges(
        owner_kind=ArtifactOwnerKind.WORKFLOW,
        owner_id="workflow-a",
        generation=1,
        content_digests=("b" * 64,),
    )
    draining = lifecycle.set_state(
        session_id,
        SessionLifecycleState.DRAINING,
        expected_generation=active.lifecycle_generation,
        expected_revision=active.revision,
    )
    terminal = lifecycle.set_state(
        session_id,
        SessionLifecycleState.TERMINAL,
        expected_generation=draining.lifecycle_generation,
        expected_revision=draining.revision,
    )
    session_dir = tmp_path / "sessions" / str(session_id)
    session_dir.mkdir(parents=True)
    (session_dir / "rollout.jsonl").write_text("evidence", encoding="utf-8")
    clock[0] += timedelta(days=31)
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("session-deletion", "maintenance", 30)
    executor = SessionDeletionExecutor(
        lifecycle, artifacts, tmp_path / "sessions", lease_coordinator=leases, lease=lease
    )
    receipt = executor.execute(
        SessionDeletionCommand(
            "delete-owned", session_id, "operator", clock[0], terminal.lifecycle_generation, terminal.revision
        )
    )
    assert receipt.state is SessionDeletionState.SETTLED
    assert not session_dir.exists()
    assert artifacts.ownership_edges(owner_kind=ArtifactOwnerKind.SESSION, owner_id=str(session_id)) == ()
    assert len(artifacts.ownership_edges(owner_kind=ArtifactOwnerKind.WORKFLOW, owner_id="workflow-a")) == 1


def test_session_stream_takeover_fences_the_previous_writer(tmp_path):
    clock = [10.0]
    coordinator = InMemoryLeaseCoordinator(clock=lambda: clock[0])
    first = SessionStreamOwnership(tmp_path, "session", coordinator=coordinator, owner_id="first", ttl_seconds=5)
    second = SessionStreamOwnership(tmp_path, "session", coordinator=coordinator, owner_id="second", ttl_seconds=5)
    with first.guard():
        assert first.lifecycle_generation == 1
    with pytest.raises(LeaseUnavailableError):
        with second.guard():
            pass
    clock[0] = 16.0
    with second.guard():
        assert second.lifecycle_generation == 2
    with pytest.raises(LeaseFencedError):
        with first.guard():
            pass
