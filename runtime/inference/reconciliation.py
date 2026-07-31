"""Pure reconciliation state authority shared by durable store adapters."""

from dataclasses import dataclass

from mote.contracts.inference.reconciliation import (
    OwnerAcknowledgement,
    OwnerDecision,
    ReconciliationState,
    ResolutionProposal,
)


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    proposal: ResolutionProposal
    state: ReconciliationState
    acknowledgement: OwnerAcknowledgement | None = None


def require_owner_action(proposal: ResolutionProposal) -> ReconciliationRecord:
    return ReconciliationRecord(
        proposal=proposal,
        state=ReconciliationState.OWNER_ACTION_REQUIRED,
    )


def acknowledge_owner_action(
    record: ReconciliationRecord,
    acknowledgement: OwnerAcknowledgement,
) -> ReconciliationRecord:
    if acknowledgement.proposal_id != record.proposal.proposal_id:
        raise ValueError("acknowledgement proposal identity mismatch")
    if acknowledgement.owner_id != record.proposal.owner_id:
        raise PermissionError("acknowledgement owner identity mismatch")
    if record.acknowledgement is not None:
        if record.acknowledgement == acknowledgement:
            return record
        raise ValueError("proposal already has a different owner acknowledgement")
    if record.state is not ReconciliationState.OWNER_ACTION_REQUIRED:
        raise ValueError("proposal is not waiting for owner action")
    state = (
        ReconciliationState.OWNER_APPLIED
        if acknowledgement.decision is OwnerDecision.APPLY
        else ReconciliationState.OWNER_REJECTED
    )
    return ReconciliationRecord(record.proposal, state, acknowledgement)


def retain_for_offline_owner(record: ReconciliationRecord) -> ReconciliationRecord:
    if record.state is not ReconciliationState.OWNER_ACTION_REQUIRED:
        raise ValueError("only unresolved owner action can remain offline")
    return record


__all__ = [
    "ReconciliationRecord",
    "acknowledge_owner_action",
    "require_owner_action",
    "retain_for_offline_owner",
]
