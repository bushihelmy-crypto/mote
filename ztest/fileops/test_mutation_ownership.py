from __future__ import annotations

import hashlib

import pytest

from mote.contracts.fileops.events import FileEditPlanStoredEvent
from mote.runtime.fileops.artifact_repository import ArtifactRepository, ArtifactWriteScopeState
from mote.runtime.fileops.control import ProjectOperationControl
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import HierarchicalLockManager
from mote.runtime.fileops.mutation_factory import MutationFactory
from mote.runtime.fileops.publisher import AtomicPublisher
from mote.runtime.fileops.review import ReviewService
from mote.runtime.fileops.snapshots import SealedSnapshotReader
from mote.runtime.fileops.transactions import DurableEditPlanArtifacts, MutationCoordinator, ScopedMutationArtifacts


def _components(tmp_path):
    repository = ArtifactRepository(
        tmp_path / "artifacts",
        hard_limit_bytes=4 * 1_024 * 1_024,
    )
    reader = SealedSnapshotReader(repository)
    locks = HierarchicalLockManager(tmp_path / "locks")
    publisher = AtomicPublisher(repository)
    journal = DurableFileOperationsJournal(
        tmp_path / "session" / "rollout.jsonl",
        session_id="ownership",
        locks=locks,
    )
    control = ProjectOperationControl(locks)
    coordinator = MutationCoordinator(
        session_id="ownership",
        artifacts=repository,
        reader=reader,
        locks=locks,
        publisher=publisher,
        journal=journal,
        control=control,
    )
    control.register(coordinator)
    factory = MutationFactory(
        session_id="ownership",
        artifacts=repository,
        get_project_root=lambda: str(tmp_path),
    )
    return repository, reader, publisher, journal, coordinator, factory


def _capture(repository, reader, target, project_root, *, encoding=None):
    scope = repository.write_scope(
        owner="test-snapshot",
        maximum_bytes=1_024 * 1_024,
        ttl_seconds=60,
    )
    with scope:
        snapshot = reader.open_snapshot(
            target,
            scope=scope,
            project_root=project_root,
            encoding=encoding,
        )
        scope.complete(durability_root=project_root)
        return snapshot


def _put(repository, project_root, data, *, owner):
    scope = repository.write_scope(
        owner=owner,
        maximum_bytes=len(data),
        ttl_seconds=60,
    )
    with scope:
        artifact = scope.put_bytes(data)
        scope.complete(durability_root=project_root)
        return artifact


def _get(repository, digest):
    return repository.read_bytes(repository.resolve_live(digest))


class _FixedReachability:
    def __init__(self, closure):
        self.closure = closure
        self.roots = ()

    def close(self, roots):
        self.roots = tuple(roots)
        return self.closure


def test_scoped_commit_completes_ownership_after_prepared_before_publish(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    repository, reader, publisher, journal, coordinator, factory = _components(tmp_path)
    snapshot = _capture(repository, reader, target, tmp_path)
    scope = repository.write_scope(
        owner="mutation",
        maximum_bytes=16,
        ttl_seconds=60,
    )
    original_publish = publisher.replace_from_blob
    observed_states = []

    def assert_completed_before_publish(*args, **kwargs):
        observed_states.append(scope.state)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(publisher, "replace_from_blob", assert_completed_before_publish)
    with scope:
        mutation = factory.replacement(snapshot, b"after", scope=scope)
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(mutation,),
        )
        result = coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
        )

    assert target.read_bytes() == b"after"
    assert result.transaction_id == mutation_set.transaction_id
    assert observed_states == [ArtifactWriteScopeState.COMPLETED]
    assert scope.state == ArtifactWriteScopeState.RELEASED
    assert journal.get(mutation_set.transaction_id) is not None


def test_scope_must_own_every_new_mutation_artifact(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    repository, reader, _, _, coordinator, factory = _components(tmp_path)
    snapshot = _capture(repository, reader, target, tmp_path)
    unowned = _put(repository, tmp_path, b"after", owner="outside-scope")
    mutation_set = factory.mutation_set(
        source="Edit",
        mutations=(factory.replacement_from_artifact(snapshot, unowned),),
    )
    scope = repository.write_scope(
        owner="empty-mutation",
        maximum_bytes=0,
        ttl_seconds=60,
    )

    with pytest.raises(ValueError, match="not owned"):
        with scope:
            coordinator.commit(mutation_set, ScopedMutationArtifacts(scope))

    assert scope.state == ArtifactWriteScopeState.ABORTED
    assert target.read_bytes() == b"before"


def test_idempotent_commit_completes_the_retry_scope_from_existing_prepared_root(
    tmp_path,
):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    repository, reader, _, journal, coordinator, factory = _components(tmp_path)
    snapshot = _capture(repository, reader, target, tmp_path)
    transaction_id = "retry-owned-transaction"

    first_scope = repository.write_scope(
        owner="first-attempt",
        maximum_bytes=5,
        ttl_seconds=60,
    )
    with first_scope:
        first = factory.mutation_set(
            source="Edit",
            transaction_id=transaction_id,
            mutations=(factory.replacement(snapshot, b"after", scope=first_scope),),
        )
        first_result = coordinator.commit(
            first,
            ScopedMutationArtifacts(first_scope),
        )

    retry_scope = repository.write_scope(
        owner="retry-attempt",
        maximum_bytes=5,
        ttl_seconds=60,
    )
    with retry_scope:
        retry = factory.mutation_set(
            source="Edit",
            transaction_id=transaction_id,
            mutations=(factory.replacement(snapshot, b"after", scope=retry_scope),),
        )
        retry_result = coordinator.commit(
            retry,
            ScopedMutationArtifacts(retry_scope),
        )

    assert retry == first
    assert retry_result == first_result
    assert retry_scope.state == ArtifactWriteScopeState.RELEASED
    assert len(tuple(journal.iter_events())) == 2


def test_hunk_artifacts_share_the_mutation_scope_and_prepared_event(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before\n")
    repository, reader, _, journal, coordinator, factory = _components(tmp_path)
    snapshot = _capture(
        repository,
        reader,
        target,
        tmp_path,
        encoding="utf-8",
    )
    scope = repository.write_scope(
        owner="mutation-with-review",
        maximum_bytes=128,
        ttl_seconds=60,
    )

    with scope:
        mutation_set = factory.mutation_set(
            source="Edit",
            mutations=(factory.replacement(snapshot, b"after\n", scope=scope),),
        )
        hunks = coordinator.build_hunks(
            mutation_set,
            turn_index=3,
            scope=scope,
        )
        coordinator.commit(
            mutation_set,
            ScopedMutationArtifacts(scope),
            hunks=hunks,
        )

    (record,) = journal.review_records()
    assert _get(repository, record.pre_hash) == b"before\n"
    assert _get(repository, record.post_hash) == b"after\n"
    assert {record.pre_hash, record.post_hash}.issubset({artifact.digest for artifact in scope.artifacts})


def test_durable_plan_ownership_requires_event_root_and_complete_closure(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    repository, reader, _, journal, coordinator, factory = _components(tmp_path)
    snapshot = _capture(repository, reader, target, tmp_path)
    after = _put(repository, tmp_path, b"after", owner="plan-after")
    mutation_set = factory.mutation_set(
        source="EditPlan",
        mutations=(factory.replacement_from_artifact(snapshot, after),),
    )
    plan_id = "a" * 64
    manifest = _put(repository, tmp_path, b"manifest", owner="plan-manifest")
    journal.append(FileEditPlanStoredEvent(plan_id, manifest))
    complete = (
        manifest,
        snapshot.artifact,
        snapshot.metadata,
        after,
    )
    reachability = _FixedReachability(complete)
    ownership = DurableEditPlanArtifacts.project(
        plan_id=plan_id,
        journal=journal,
        reachability=reachability,
    )

    coordinator.commit(mutation_set, ownership)

    assert target.read_bytes() == b"after"
    assert reachability.roots[0].artifact == manifest
    assert reachability.roots[0].identity == plan_id


def test_durable_plan_ownership_rejects_mutation_outside_manifest_closure(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    repository, reader, _, journal, coordinator, factory = _components(tmp_path)
    snapshot = _capture(repository, reader, target, tmp_path)
    after = _put(repository, tmp_path, b"after", owner="plan-after")
    mutation_set = factory.mutation_set(
        source="EditPlan",
        mutations=(factory.replacement_from_artifact(snapshot, after),),
    )
    plan_id = "b" * 64
    manifest = _put(repository, tmp_path, b"manifest", owner="plan-manifest")
    journal.append(FileEditPlanStoredEvent(plan_id, manifest))
    ownership = DurableEditPlanArtifacts.project(
        plan_id=plan_id,
        journal=journal,
        reachability=_FixedReachability((manifest,)),
    )

    with pytest.raises(ValueError, match="outside"):
        coordinator.commit(mutation_set, ownership)

    assert target.read_bytes() == b"before"


def test_review_delta_scope_covers_artifacts_until_hunk_event_is_durable(tmp_path):
    repository, _, _, journal, _, _ = _components(tmp_path)
    review = ReviewService(session_id="ownership", journal=journal)
    scope = repository.write_scope(
        owner="review-delta",
        maximum_bytes=64,
        ttl_seconds=60,
    )

    with scope:
        records = review.record_delta(
            path="target.txt",
            old="before\n",
            new="after\n",
            source="agent",
            turn_index=1,
            id_base="review",
            expected_digest=hashlib.sha256(b"after\n").hexdigest(),
            scope=scope,
        )
        assert scope.state == ArtifactWriteScopeState.COMPLETED

    assert len(records) == 1
    assert scope.state == ArtifactWriteScopeState.RELEASED
    assert _get(repository, records[0].pre_hash) == b"before\n"
    assert _get(repository, records[0].post_hash) == b"after\n"
    assert journal.review(records[0].hunk_id) == records[0]
