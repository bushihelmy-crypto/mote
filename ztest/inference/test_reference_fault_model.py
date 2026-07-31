"""Gate 0 reference model; deliberately not imported by production code."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import pytest

from mote.contracts.inference.events import AttemptEventType
from mote.contracts.inference.receipt import ReceiptState
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit

DIGEST = "sha256:" + "c" * 64


class CrashPoint(StrEnum):
    NONE = "none"
    AFTER_INTENT = "after_intent"
    AFTER_COMMIT = "after_commit"
    AFTER_WIRE = "after_wire"


class SimulatedCrash(RuntimeError):
    pass


@dataclass
class FakeProvider:
    requests: int = 0

    async def send_once(self) -> dict[str, str]:
        self.requests += 1
        if self.requests > 1:
            raise AssertionError("wire unit sent more than once")
        return {"id": "provider-request-1", "content": "ok"}


@dataclass
class ReferenceResult:
    events: list[AttemptEventType]
    receipt_state: ReceiptState
    wire_requests: int


class ReferenceAttempt:
    def __init__(self, provider: FakeProvider, *, attempt_id: str = "attempt-1") -> None:
        self.provider = provider
        self.attempt_id = attempt_id
        self.receipt_state = ReceiptState.ACCEPTED
        self.consumed_permit_digest: str | None = None

    async def run(self, permit: WirePermit, crash_at: CrashPoint) -> ReferenceResult:
        events = [
            AttemptEventType.QUEUED,
            AttemptEventType.BUDGET_RESERVED,
            AttemptEventType.WIRE_PREPARED,
            AttemptEventType.WIRE_AUTHORIZATION_REQUIRED,
        ]
        self._validate_permit(permit)
        self.receipt_state = ReceiptState.SEND_INTENT_DURABLE
        if crash_at is CrashPoint.AFTER_INTENT:
            raise SimulatedCrash(crash_at)
        self._consume_permit(permit)
        self.receipt_state = ReceiptState.SEND_COMMITTED
        events.append(AttemptEventType.SEND_COMMITTED)
        if crash_at is CrashPoint.AFTER_COMMIT:
            raise SimulatedCrash(crash_at)
        events.append(AttemptEventType.WIRE_STARTED)
        await self.provider.send_once()
        self.receipt_state = ReceiptState.WIRE_STARTED_OBSERVED
        if crash_at is CrashPoint.AFTER_WIRE:
            raise SimulatedCrash(crash_at)
        events.extend((AttemptEventType.RESPONSE_STARTED, AttemptEventType.SUCCEEDED))
        self.receipt_state = ReceiptState.TERMINAL_SUCCEEDED
        return ReferenceResult(events, self.receipt_state, self.provider.requests)

    def reconcile_after_crash(self) -> ReceiptState:
        if self.receipt_state is ReceiptState.SEND_INTENT_DURABLE:
            return self.receipt_state
        if self.receipt_state in {ReceiptState.SEND_COMMITTED, ReceiptState.WIRE_STARTED_OBSERVED}:
            self.receipt_state = ReceiptState.IN_DOUBT
        return self.receipt_state

    def _validate_permit(self, permit: WirePermit) -> None:
        assert permit.attempt_id == self.attempt_id
        assert permit.generation_id == "generation-1"
        assert permit.execution_taxonomy is ExecutionTaxonomy.UNARY_FINITE_ATTEMPT

    def _consume_permit(self, permit: WirePermit) -> None:
        digest = permit.generation_artifact_digest + f":{permit.nonce}"
        if self.consumed_permit_digest is not None and self.consumed_permit_digest != digest:
            raise ValueError("conflicting permit")
        self.consumed_permit_digest = digest


def _permit(
    *,
    attempt_id: str = "attempt-1",
    ordinal: int = 1,
    nonce: str = "0123456789abcdef",
) -> WirePermit:
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id=attempt_id,
        execution_taxonomy="unary_finite_attempt",
        owner_journal_id="journal-1",
        wire_unit="http-request",
        generation_id="generation-1",
        generation_artifact_digest=DIGEST,
        ordinal=ordinal,
        nonce=nonce,
        issued_journal_revision=ordinal,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key-1",
        audience="embedded/application-1/tenant-1",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="embedded-capability",
    )


def test_reference_success_has_ordered_commit_boundary_and_one_wire_request():
    async def scenario():
        result = await ReferenceAttempt(FakeProvider()).run(_permit(), CrashPoint.NONE)
        assert result.events.index(AttemptEventType.SEND_COMMITTED) < result.events.index(AttemptEventType.WIRE_STARTED)
        assert result.receipt_state is ReceiptState.TERMINAL_SUCCEEDED
        assert result.wire_requests == 1

    asyncio.run(scenario())


def test_hedged_attempts_use_distinct_ordinals_and_permits():
    async def scenario():
        first_permit = _permit(attempt_id="attempt-hedge-1", ordinal=1)
        second_permit = _permit(
            attempt_id="attempt-hedge-2",
            ordinal=2,
            nonce="fedcba9876543210",
        )
        assert first_permit.ordinal != second_permit.ordinal
        assert first_permit.nonce != second_permit.nonce

        first_provider = FakeProvider()
        second_provider = FakeProvider()
        await asyncio.gather(
            ReferenceAttempt(first_provider, attempt_id=first_permit.attempt_id).run(first_permit, CrashPoint.NONE),
            ReferenceAttempt(second_provider, attempt_id=second_permit.attempt_id).run(second_permit, CrashPoint.NONE),
        )
        assert (first_provider.requests, second_provider.requests) == (1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("crash_at", "wire_count", "reconciled"),
    [
        (CrashPoint.AFTER_INTENT, 0, ReceiptState.SEND_INTENT_DURABLE),
        (CrashPoint.AFTER_COMMIT, 0, ReceiptState.IN_DOUBT),
        (CrashPoint.AFTER_WIRE, 1, ReceiptState.IN_DOUBT),
    ],
)
def test_reference_crash_points_never_exceed_one_wire(crash_at, wire_count, reconciled):
    async def scenario():
        provider = FakeProvider()
        attempt = ReferenceAttempt(provider)
        with pytest.raises(SimulatedCrash):
            await attempt.run(_permit(), crash_at)
        assert provider.requests == wire_count
        assert attempt.reconcile_after_crash() is reconciled

    asyncio.run(scenario())
