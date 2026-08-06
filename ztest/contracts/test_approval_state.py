from dataclasses import fields

from mote.contracts.execution.pending_act import PendingAction
from mote.contracts.interaction.approval import ApprovalState
from mote.contracts.tool.external_effect import ExternalEffectState


def test_approval_state_is_not_an_execution_state_machine() -> None:
    assert set(ApprovalState).isdisjoint(set(ExternalEffectState))
    assert "IN_DOUBT" not in ApprovalState.__members__
    assert "approval_state" not in {field.name for field in fields(PendingAction)}
