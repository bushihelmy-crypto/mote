from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

from mote.contracts.fileops.models import FileOperationsHealth
from mote.runtime.fileops.artifact_gc import (
    ArtifactDeletionPass,
    ArtifactGarbageCollectionCycle,
    ArtifactQuarantinePass,
    ArtifactReclamationPass,
)
from mote.runtime.fileops.facade import FileOperations
from mote.runtime.fileops.gc_service import ArtifactGarbageCollectionService


def _operations(tmp_path):
    return FileOperations(
        session_id="gc-service",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=tmp_path / "locks",
    )


def _empty_cycle() -> ArtifactGarbageCollectionCycle:
    return ArtifactGarbageCollectionCycle(
        quarantine=ArtifactQuarantinePass(0, 0, 0, (), ()),
        deletion=ArtifactDeletionPass(0, 0, 0, (), (), ()),
        reclamation=ArtifactReclamationPass((), ()),
    )


def _healthy() -> FileOperationsHealth:
    return FileOperationsHealth(
        lock_backend="test",
        journal_readable=True,
        journal_writable=True,
        artifact_readable=True,
        artifact_writable=True,
        artifact_catalog_readable=True,
        recovery_backlog=0,
    )


class _Target:
    def __init__(self) -> None:
        self.called = threading.Event()
        self.health_called = threading.Event()
        self.collection_thread: int | None = None
        self.health_thread: int | None = None
        self.expedited: list[bool] = []

    def collect_artifacts(self, *, limit, expedited):
        self.collection_thread = threading.get_ident()
        self.expedited.append(expedited)
        self.called.set()
        return _empty_cycle()

    def health(self):
        self.health_thread = threading.get_ident()
        self.health_called.set()
        return _healthy()


def test_run_once_collects_through_the_file_operations_facade(tmp_path):
    operations = _operations(tmp_path)
    with operations.artifacts.write_scope(
        owner="gc-service-unrooted",
        maximum_bytes=7,
        ttl_seconds=60,
    ) as scope:
        scope.put_bytes(b"garbage")
        scope.discard()
    service = ArtifactGarbageCollectionService(operations, batch_size=1)

    cycle = asyncio.run(service.run_once())

    assert len(cycle.quarantine.quarantined_objects) == 1
    assert operations.health().artifact_quarantined_objects == 1


def test_service_starts_immediately_and_joins_on_close():
    target = _Target()

    async def run():
        event_loop_thread = threading.get_ident()
        service = ArtifactGarbageCollectionService(target, batch_size=1)
        service.start()
        assert await asyncio.to_thread(target.called.wait, 2)
        assert await asyncio.to_thread(target.health_called.wait, 2)
        assert target.collection_thread != event_loop_thread
        assert target.health_thread != event_loop_thread
        await service.aclose()

    asyncio.run(run())


def test_close_waits_for_the_inflight_bounded_collection():
    entered = threading.Event()
    release = threading.Event()

    class BlockingTarget(_Target):
        def collect_artifacts(self, *, limit, expedited):
            entered.set()
            assert release.wait(2)
            return _empty_cycle()

    async def run():
        service = ArtifactGarbageCollectionService(BlockingTarget(), batch_size=1)
        service.start()
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, timeout=2)

    asyncio.run(run())


def test_quota_pressure_requests_expedited_collection():
    class PressureTarget(_Target):
        def health(self):
            return replace(
                _healthy(),
                artifact_quota_pressure=0.8,
            )

    target = PressureTarget()

    async def run():
        service = ArtifactGarbageCollectionService(target, batch_size=1)
        service.start()
        assert await asyncio.to_thread(target.called.wait, 2)
        await service.aclose()

    asyncio.run(run())
    assert target.expedited == [True]
