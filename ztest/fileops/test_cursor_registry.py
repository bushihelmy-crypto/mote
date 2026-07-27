from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import threading

import pytest

from mote.contracts.fileops.errors import ReadCursorError
from mote.contracts.fileops.models import BlobRef
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry

_IDLE_TTL = 10
_HARD_TTL = 25


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _ref(character: str, size: int) -> BlobRef:
    return BlobRef(digest=character * 64, size=size)


def _registry(tmp_path, clock=None) -> DurableCursorRegistry:
    return DurableCursorRegistry(
        tmp_path / "cursor-registry.sqlite3",
        idle_ttl_ns=_IDLE_TTL,
        hard_ttl_ns=_HARD_TTL,
        now_ns=clock or _Clock(100),
    )


def _invalidate_process(path: str, ready, start, outcomes) -> None:
    registry = DurableCursorRegistry(
        path,
        idle_ttl_ns=_IDLE_TTL,
        hard_ttl_ns=_HARD_TTL,
        now_ns=lambda: 100,
    )
    ready.put(True)
    start.wait(5)
    outcomes.put(registry.invalidate().epoch)


def _advance_process(path: str, token: str, ready, start, outcomes) -> None:
    registry = DurableCursorRegistry(
        path,
        idle_ttl_ns=_IDLE_TTL,
        hard_ttl_ns=_HARD_TTL,
        now_ns=lambda: 101,
    )
    ready.put(True)
    start.wait(5)
    opened = registry.open(token, expected_namespace="read")
    next_token = registry.advance(
        token,
        expected_namespace="read",
        position=opened.position + 1,
    )
    outcomes.put((opened.position, next_token))


def test_issue_persists_an_opaque_capability_and_canonical_pin_closure(tmp_path):
    registry = _registry(tmp_path)
    root = _ref("a", 7)
    dependency = _ref("b", 11)

    token = registry.issue(
        namespace="read",
        root_manifest=root,
        pinned_artifacts=(dependency, root, dependency),
        position=37,
        expected_epoch=0,
    )
    reopened = _registry(tmp_path).open(token, expected_namespace="read")

    assert len(token) == 43
    assert root.digest not in token
    assert "37" not in token
    assert reopened.position == 37
    assert reopened.lease.root_manifest == root
    assert reopened.lease.pinned_artifacts == (root, dependency)
    assert reopened.lease.epoch == registry.current_epoch == 0


def test_open_renews_idle_expiry_without_crossing_the_hard_deadline(tmp_path):
    clock = _Clock(100)
    registry = _registry(tmp_path, clock)
    token = registry.issue(
        namespace="search",
        root_manifest=_ref("a", 1),
        pinned_artifacts=(),
        position=0,
        expected_epoch=0,
    )

    clock.value = 109
    first = registry.open(token, expected_namespace="search")
    clock.value = 118
    second = registry.open(token, expected_namespace="search")

    assert first.lease.expires_at_ns == 119
    assert second.lease.expires_at_ns == 125
    assert second.lease.hard_expires_at_ns == 125
    assert second.lease.revision == 3
    clock.value = 125
    with pytest.raises(ReadCursorError, match="expired"):
        registry.open(token, expected_namespace="search")


def test_advance_is_deterministic_idempotent_and_durable(tmp_path):
    registry = _registry(tmp_path)
    token = registry.issue(
        namespace="read",
        root_manifest=_ref("a", 1),
        pinned_artifacts=(),
        position=4,
        expected_epoch=0,
    )

    first = registry.advance(token, expected_namespace="read", position=8)
    second = registry.advance(token, expected_namespace="read", position=8)
    reopened = _registry(tmp_path).open(first, expected_namespace="read")

    assert first == second
    assert first != token
    assert reopened.position == 8


def test_pin_snapshot_has_a_monotonic_root_set_revision(tmp_path):
    registry = _registry(tmp_path)
    initial = registry.pin_snapshot()
    root = _ref("a", 7)
    dependency = _ref("b", 11)

    token = registry.issue(
        namespace="read",
        root_manifest=root,
        pinned_artifacts=(dependency,),
        position=1,
        expected_epoch=0,
    )
    issued = registry.pin_snapshot()
    registry.advance(token, expected_namespace="read", position=2)
    advanced = registry.pin_snapshot()
    registry.release(token, expected_namespace="read")
    released = registry.pin_snapshot()

    assert initial.artifacts == ()
    assert issued.artifacts == (root, dependency)
    assert issued.revision == initial.revision + 1
    assert advanced == issued
    assert released.artifacts == ()
    assert released.revision == issued.revision + 1


def test_namespace_release_and_malformed_tokens_are_enforced(tmp_path):
    registry = _registry(tmp_path)
    token = registry.issue(
        namespace="read",
        root_manifest=_ref("a", 1),
        pinned_artifacts=(),
        position=0,
        expected_epoch=0,
    )

    with pytest.raises(ReadCursorError, match="namespace"):
        registry.open(token, expected_namespace="search")
    with pytest.raises(ReadCursorError, match="invalid"):
        registry.open(token[:-1] + "=", expected_namespace="read")
    registry.release(token, expected_namespace="read")
    registry.release(token, expected_namespace="read")
    with pytest.raises(ReadCursorError, match="released"):
        registry.open(token, expected_namespace="read")


def test_invalidate_and_synchronize_are_monotonic_epoch_fences(tmp_path):
    registry = _registry(tmp_path)
    old = registry.issue(
        namespace="read",
        root_manifest=_ref("a", 1),
        pinned_artifacts=(),
        position=0,
        expected_epoch=0,
    )

    assert registry.invalidate().epoch == 1
    with pytest.raises(ReadCursorError, match="stale timeline"):
        registry.open(old, expected_namespace="read")
    with pytest.raises(ReadCursorError, match="source belongs to a stale"):
        registry.issue(
            namespace="read",
            root_manifest=_ref("c", 3),
            pinned_artifacts=(),
            position=0,
            expected_epoch=0,
        )
    assert registry.synchronize(7).epoch == 7
    assert registry.synchronize(3).epoch == 7
    assert _registry(tmp_path).current_epoch == 7

    current = registry.issue(
        namespace="read",
        root_manifest=_ref("b", 2),
        pinned_artifacts=(),
        position=9,
        expected_epoch=7,
    )
    assert registry.open(current, expected_namespace="read").lease.epoch == 7


def test_concurrent_process_invalidations_are_serialized_without_lost_updates(
    tmp_path,
):
    path = tmp_path / "cursor-registry.sqlite3"
    DurableCursorRegistry(
        path,
        idle_ttl_ns=_IDLE_TTL,
        hard_ttl_ns=_HARD_TTL,
        now_ns=lambda: 100,
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_invalidate_process,
            args=(str(path), ready, start, outcomes),
        )
        for _ in range(4)
    )
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10)
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    epochs = sorted(outcomes.get(timeout=2) for _ in processes)
    assert epochs == [1, 2, 3, 4]
    assert (
        DurableCursorRegistry(
            path,
            idle_ttl_ns=_IDLE_TTL,
            hard_ttl_ns=_HARD_TTL,
            now_ns=lambda: 100,
        ).current_epoch
        == 4
    )


def test_concurrent_process_retries_produce_the_same_continuation(tmp_path):
    registry = _registry(tmp_path)
    token = registry.issue(
        namespace="read",
        root_manifest=_ref("a", 1),
        pinned_artifacts=(),
        position=5,
        expected_epoch=0,
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_advance_process,
            args=(str(registry.path), token, ready, start, outcomes),
        )
        for _ in range(2)
    )
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10)
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    results = tuple(outcomes.get(timeout=2) for _ in processes)
    assert results[0][0] == results[1][0] == 5
    assert results[0][1] == results[1][1]
    assert registry.open(results[0][1], expected_namespace="read").position == 6


def test_registry_rejects_noncanonical_schema_without_runtime_migration(tmp_path):
    path = tmp_path / "cursor-registry.sqlite3"
    _registry(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE compatibility_residue (value TEXT)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ReadCursorError, match="not canonical"):
        DurableCursorRegistry(
            path,
            idle_ttl_ns=_IDLE_TTL,
            hard_ttl_ns=_HARD_TTL,
            now_ns=lambda: 100,
        )


def test_frozen_pin_snapshot_blocks_concurrent_pin_publication(tmp_path):
    registry = _registry(tmp_path)
    outcomes = queue.Queue()

    def publish_pin():
        try:
            outcomes.put(
                registry.issue(
                    namespace="read",
                    root_manifest=_ref("a", 1),
                    pinned_artifacts=(),
                    position=0,
                    expected_epoch=0,
                )
            )
        except Exception as exc:
            outcomes.put(exc)

    with registry.freeze_pins() as snapshot:
        worker = threading.Thread(target=publish_pin)
        worker.start()
        with pytest.raises(queue.Empty):
            outcomes.get(timeout=0.2)
        assert snapshot.artifacts == ()

    worker.join(5)
    outcome = outcomes.get(timeout=1)
    assert isinstance(outcome, str)
    assert registry.pin_snapshot().revision > snapshot.revision


@pytest.mark.parametrize("value", [-1, True, 1 << 63])
def test_positions_and_epochs_use_strict_bounded_integers(tmp_path, value):
    registry = _registry(tmp_path)
    with pytest.raises(ReadCursorError):
        registry.issue(
            namespace="read",
            root_manifest=_ref("a", 1),
            pinned_artifacts=(),
            position=value,
            expected_epoch=0,
        )
    with pytest.raises(ReadCursorError):
        registry.synchronize(value)
