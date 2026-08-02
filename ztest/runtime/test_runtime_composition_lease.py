"""Typed ownership semantics for borrowed Runtime generation capabilities."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from mote.contracts.runtime.application import (
    DefaultModelView,
    RuntimeGenerationId,
    RuntimeLeaseHolderId,
    RuntimeLeaseReleaseDisposition,
)
from mote.runtime.models.composition import (
    LeaseReleasedError,
    RuntimeCompositionGeneration,
    SharedRuntimeCompositionHandle,
)


class _Closable:
    def __init__(self) -> None:
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


def _handle(closeable: _Closable) -> SharedRuntimeCompositionHandle:
    generation = RuntimeCompositionGeneration(
        runtime_generation_id=RuntimeGenerationId("runtime-1"),
        topology_revision="topology-1",
        gateway=cast(object, object()),
        route_policy=cast(object, object()),
        default_model=DefaultModelView("model", "provider", "transport", 1),
        command_runtime=None,
        session_runtime=None,
        transfer_runtime=None,
        permit_issuer=None,
        epoch_source=None,
        permit_audience="audience",
        generation_id="generation-1",
        generation_artifact_digest="sha256:digest",
        artifact_store=None,
        artifact_reader=None,
        _runtime_generation=cast(object, SimpleNamespace(closeables=(closeable,))),
    )
    return SharedRuntimeCompositionHandle(generation, reuse_key="runtime-1")


@pytest.mark.asyncio
async def test_runtime_lease_transfer_invalidates_old_holder_and_closes_once() -> None:
    closeable = _Closable()
    handle = _handle(closeable)
    lease = await handle.acquire()
    old_holder = lease.holder_id
    new_holder = RuntimeLeaseHolderId("replacement-incarnation")

    replacement, transfer = await lease.transfer(new_holder)
    assert transfer.previous_holder_id == old_holder
    assert transfer.holder_id == new_holder
    assert (await lease.aclose()).disposition is RuntimeLeaseReleaseDisposition.TRANSFERRED
    with pytest.raises(LeaseReleasedError):
        _ = lease.gateway

    await handle.release()
    assert closeable.closes == 0
    assert (await replacement.aclose()).disposition is RuntimeLeaseReleaseDisposition.RELEASED
    assert closeable.closes == 1
    assert (await replacement.aclose()).disposition is RuntimeLeaseReleaseDisposition.ALREADY_RELEASED
    assert closeable.closes == 1


@pytest.mark.asyncio
async def test_runtime_lease_rejects_same_holder_transfer() -> None:
    handle = _handle(_Closable())
    lease = await handle.acquire()
    with pytest.raises(ValueError, match="new holder"):
        await lease.transfer(lease.holder_id)
    await lease.aclose()
    await handle.release()
