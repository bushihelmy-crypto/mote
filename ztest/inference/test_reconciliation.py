from datetime import datetime, timezone

import pytest

from mote.contracts.inference.reconciliation import (
    OwnerAcknowledgement,
    OwnerDecision,
    ReconciliationState,
    ResolutionProposal,
)
from mote.runtime.inference.reconciliation import (
    acknowledge_owner_action,
    require_owner_action,
    retain_for_offline_owner,
)


def _proposal():
    return ResolutionProposal(
        proposal_id="proposal-1",
        owner_id="caller-1",
        execution_id="attempt-1",
        generation_id="generation-1",
        strategy_id="provider-query-v1",
        evidence_digests=("sha256:" + "a" * 64,),
        created_at=datetime.now(timezone.utc),
    )


def test_owner_acknowledgement_replay_is_idempotent_and_conflicts_fail_closed():
    record = require_owner_action(_proposal())
    acknowledgement = OwnerAcknowledgement(
        proposal_id="proposal-1",
        owner_id="caller-1",
        decision=OwnerDecision.APPLY,
        owner_journal_revision=7,
    )
    applied = acknowledge_owner_action(record, acknowledgement)
    assert applied.state is ReconciliationState.OWNER_APPLIED
    assert acknowledge_owner_action(applied, acknowledgement) is applied

    with pytest.raises(ValueError, match="different owner acknowledgement"):
        acknowledge_owner_action(
            applied,
            acknowledgement.model_copy(update={"owner_journal_revision": 8}),
        )


def test_permanently_offline_owner_remains_explicitly_unresolved():
    record = require_owner_action(_proposal())
    retained = retain_for_offline_owner(record)
    assert retained is record
    assert retained.state is ReconciliationState.OWNER_ACTION_REQUIRED
    assert retained.acknowledgement is None
