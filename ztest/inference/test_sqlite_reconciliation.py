import asyncio
from datetime import datetime, timezone

import pytest

from mote.contracts.inference.provider_evidence import ProviderEvidence, ProviderEvidenceConflictError
from mote.contracts.inference.reconciliation import OwnerAcknowledgement, OwnerDecision, ReconciliationState
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteReconciliationAuthority


def _evidence(
    *,
    event_id="event-1",
    event_type="completed",
    occurred_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
):
    return ProviderEvidence(
        provider="openai",
        event_id=event_id,
        execution_id="execution-1",
        event_type=event_type,
        occurred_at=occurred_at,
        provider_resource_id="provider-secret-id",
    )


async def _authority(path):
    receipts = SQLiteAttemptReceiptStore(path)
    await receipts.initialize()
    authority = SQLiteReconciliationAuthority(receipts)
    await authority.bind_execution(
        execution_id="execution-1",
        generation_id="generation-1",
        provider="openai",
        owner_id="owner-1",
        strategy_id="verified-webhook-v1",
    )
    return authority


def test_provider_evidence_is_durable_idempotent_and_conflicts_fail_closed(tmp_path):
    async def scenario():
        path = tmp_path / "gateway.sqlite3"
        authority = await _authority(path)
        assert await authority.append(_evidence())
        assert not await authority.append(_evidence())
        with pytest.raises(ProviderEvidenceConflictError, match="reused"):
            await authority.append(_evidence(event_type="failed"))

        restarted = SQLiteReconciliationAuthority(SQLiteAttemptReceiptStore(path))
        evidence = await restarted.list_evidence("execution-1")
        return evidence

    evidence = asyncio.run(scenario())
    assert len(evidence) == 1
    assert evidence[0].generation_id == "generation-1"
    assert evidence[0].digest.startswith("sha256:")


def test_proposal_and_owner_acknowledgement_survive_restart(tmp_path):
    async def scenario():
        path = tmp_path / "gateway.sqlite3"
        authority = await _authority(path)
        await authority.append(_evidence())
        proposal = await authority.propose("execution-1")
        assert await authority.propose("execution-1") == proposal
        outbox = await authority.read_outbox()
        assert len(outbox) == 1

        acknowledgement = OwnerAcknowledgement(
            proposal_id=proposal.proposal.proposal_id,
            owner_id="owner-1",
            decision=OwnerDecision.APPLY,
            owner_journal_revision=8,
        )
        applied = await authority.acknowledge(acknowledgement)
        assert await authority.acknowledge(acknowledgement) == applied
        with pytest.raises(ValueError, match="different owner acknowledgement"):
            await authority.acknowledge(acknowledgement.model_copy(update={"owner_journal_revision": 9}))

        restarted = SQLiteReconciliationAuthority(SQLiteAttemptReceiptStore(path))
        return await restarted.get("execution-1")

    record = asyncio.run(scenario())
    assert record is not None
    assert record.state is ReconciliationState.OWNER_APPLIED
    assert record.acknowledgement is not None


def test_offline_owner_remains_in_durable_action_required_backlog(tmp_path):
    async def scenario():
        authority = await _authority(tmp_path / "gateway.sqlite3")
        await authority.append(_evidence())
        await authority.propose("execution-1")
        return await authority.list_records(state=ReconciliationState.OWNER_ACTION_REQUIRED)

    records = asyncio.run(scenario())
    assert len(records) == 1
    assert records[0].acknowledgement is None


def test_proposal_snapshots_evidence_and_resource_identity_cannot_drift(tmp_path):
    async def scenario():
        authority = await _authority(tmp_path / "gateway.sqlite3")
        await authority.append(_evidence())
        with pytest.raises(ProviderEvidenceConflictError, match="resource identity"):
            await authority.append(
                _evidence(event_id="event-2").model_copy(update={"provider_resource_id": "different-resource"})
            )
        await authority.propose("execution-1")
        with pytest.raises(ProviderEvidenceConflictError, match="proposal snapshot"):
            await authority.append(_evidence(event_id="event-3"))

    asyncio.run(scenario())
