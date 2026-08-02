from __future__ import annotations

import asyncio

import pytest

from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError
from mote.contracts.runtime.lease import RuntimeLeasePolicy
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    OperationBackend,
    OperationOwnershipRequest,
    project_operation_guarantee,
)
from mote.runtime.control.leases import InMemoryLeaseCoordinator, LeaseHandle
from mote.runtime.control.operation_ownership import LeaseOperationOwnership


def _request(holder: str, capability: EffectCapability = EffectCapability.NO_EXTERNAL_EFFECT):
    return OperationOwnershipRequest(
        "deployment",
        "operation",
        holder,
        OperationBackend.LOCAL_FILE,
        3,
        "effect",
        capability,
    )


def test_claim_takeover_and_stale_mutation_are_fenced() -> None:
    now = [10.0]
    leases = InMemoryLeaseCoordinator(clock=lambda: now[0])
    owner = LeaseOperationOwnership(leases, backend=OperationBackend.LOCAL_FILE)
    old = owner.claim(_request("old"), 1)
    with pytest.raises(Exception):
        owner.claim(_request("other"), 1)
    now[0] = 12.0
    current = owner.claim(_request("other"), 10)
    assert current.fencing_token > old.fencing_token
    with pytest.raises(Exception):
        owner.assert_current(old)
    with owner.guard(current):
        pass


def test_backend_mismatch_fails_closed() -> None:
    owner = LeaseOperationOwnership(InMemoryLeaseCoordinator(), backend=OperationBackend.LOCAL_FILE)
    request = OperationOwnershipRequest(
        "deployment",
        "operation",
        "worker",
        OperationBackend.TEMPORAL_HISTORY,
        0,
        "effect",
        EffectCapability.IDEMPOTENT_BY_KEY,
    )
    with pytest.raises(ValueError, match="backend"):
        owner.claim(request, 10)


@pytest.mark.asyncio
async def test_lease_handle_adopts_and_renews_an_existing_epoch() -> None:
    coordinator = InMemoryLeaseCoordinator()
    lease = coordinator.acquire("subject", "owner", 0.06)
    handle = LeaseHandle(
        coordinator,
        subject="subject",
        owner_id="owner",
        policy=RuntimeLeasePolicy(0.06, 0.02),
    )
    await handle.adopt(lease)
    await asyncio.sleep(0.05)
    handle.assert_current()
    assert handle.fencing_token == lease.fencing_token
    await handle.close()


@pytest.mark.asyncio
async def test_lease_handle_reports_renewal_loss_fail_closed() -> None:
    class _RenewalFailure(InMemoryLeaseCoordinator):
        def renew(self, lease, ttl_seconds):
            raise OSError("coordinator unavailable")

    coordinator = _RenewalFailure()
    lease = coordinator.acquire("subject", "owner", 1)
    handle = LeaseHandle(
        coordinator,
        subject="subject",
        owner_id="owner",
        policy=RuntimeLeasePolicy(1, 0.01),
    )
    await handle.adopt(lease)
    with pytest.raises(LeaseCoordinatorUnavailableError, match="heartbeat failed"):
        await handle.wait_for_loss()
    await handle.close()


@pytest.mark.parametrize(
    ("capability", "retry", "reconcile"),
    [
        (EffectCapability.NO_EXTERNAL_EFFECT, True, False),
        (EffectCapability.IDEMPOTENT_BY_KEY, True, False),
        (EffectCapability.RECONCILABLE_BY_RECEIPT, False, True),
        (EffectCapability.NON_REPLAYABLE, False, True),
    ],
)
def test_effect_guarantee_never_overclaims_provider_capability(capability, retry, reconcile):
    guarantee = project_operation_guarantee(OperationBackend.TEMPORAL_HISTORY, capability)
    assert guarantee.automatic_retry_allowed is retry
    assert guarantee.reconciliation_required is reconcile
