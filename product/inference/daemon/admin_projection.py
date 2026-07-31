"""Production admin read model over daemon-owned durable authorities."""

from __future__ import annotations

from dataclasses import asdict

from mote.contracts.inference.reconciliation import ReconciliationState
from mote.product.inference.admin_model import AdminReadModel
from mote.runtime.inference.receipt_projection import ReceiptProjection


def build_daemon_admin_read_model(
    *,
    receipts,
    readiness,
    audit,
    reconciliation_authority=None,
    providers=(),
    credentials=(),
    generations=(),
) -> AdminReadModel:
    projection = ReceiptProjection(receipts)

    async def constant(values):
        return tuple(values)

    async def read_readiness():
        ready, components = readiness()
        return {"ready": ready, "components": dict(components)}

    async def receipt(execution_id: str):
        candidates = await receipts.list_receipts(limit=1000)
        matches = [item for item in candidates if item.attempt_id == execution_id]
        if not matches:
            return None
        latest = max(matches, key=lambda item: (item.updated_at, item.revision))
        return await projection.get(latest.attempt_id, latest.generation_id)

    async def reconciliation():
        receipt_backlog = tuple(asdict(item) for item in await projection.reconciliation())
        if reconciliation_authority is None:
            return receipt_backlog
        records = await reconciliation_authority.list_records(
            state=ReconciliationState.OWNER_ACTION_REQUIRED, limit=1000
        )
        proposals = tuple(
            {
                "execution_id": record.proposal.execution_id,
                "generation_id": record.proposal.generation_id,
                "proposal_id": record.proposal.proposal_id,
                "owner_id": record.proposal.owner_id,
                "strategy_id": record.proposal.strategy_id,
                "state": record.state.value,
                "evidence_count": len(record.proposal.evidence_digests),
                "created_at": record.proposal.created_at.isoformat(),
            }
            for record in records
        )
        proposed_ids = {item["execution_id"] for item in proposals}
        return proposals + tuple(item for item in receipt_backlog if item["execution_id"] not in proposed_ids)

    async def read_audit(after: int):
        return tuple([item async for item in audit.read(after=after)])

    return AdminReadModel(
        providers=lambda: constant(providers),
        credentials=lambda: constant(credentials),
        generations=lambda: constant(generations),
        readiness=read_readiness,
        receipt=receipt,
        reconciliation=reconciliation,
        audit=read_audit,
    )


__all__ = ["build_daemon_admin_read_model"]
