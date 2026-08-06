from mote.contracts.interaction.approval import ApprovalState
from mote.contracts.tool.external_effect import ExternalEffectState


def test_external_effect_state_keeps_independent_started_and_in_doubt_facts() -> None:
    assert ExternalEffectState.STARTED.value == "started"
    assert ExternalEffectState.IN_DOUBT.value == "in_doubt"
    assert not hasattr(ApprovalState, "STARTED")
    assert not hasattr(ApprovalState, "IN_DOUBT")
