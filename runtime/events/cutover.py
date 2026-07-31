"""Fail-closed validation for durable store cutover histories."""

from __future__ import annotations

from collections.abc import Sequence

from mote.contracts.events.governance import CutoverDeclaration, CutoverState, CutoverTransition

_TRANSITIONS = {
    CutoverState.PREPARED: {CutoverState.WRITER_FENCED, CutoverState.ABORTED_PRE_FENCE},
    CutoverState.WRITER_FENCED: {
        CutoverState.QUIESCED,
        CutoverState.BLOCKED_POST_FENCE,
    },
    CutoverState.QUIESCED: {
        CutoverState.MIGRATED,
        CutoverState.FAILED_VALIDATION,
        CutoverState.BLOCKED_POST_FENCE,
    },
    CutoverState.FAILED_VALIDATION: {
        CutoverState.MIGRATED,
        CutoverState.BLOCKED_POST_FENCE,
    },
    CutoverState.BLOCKED_POST_FENCE: {
        CutoverState.QUIESCED,
        CutoverState.MIGRATED,
        CutoverState.FAILED_VALIDATION,
    },
    CutoverState.MIGRATED: {CutoverState.ACTIVATED},
    CutoverState.ACTIVATED: {CutoverState.OBSERVED, CutoverState.CLEANUP_BLOCKED},
    CutoverState.OBSERVED: {CutoverState.CLEANED, CutoverState.CLEANUP_BLOCKED},
    CutoverState.CLEANUP_BLOCKED: {CutoverState.OBSERVED, CutoverState.CLEANED},
}
_TERMINAL = {CutoverState.ABORTED_PRE_FENCE, CutoverState.CLEANED}


def validate_cutover_history(
    declaration: CutoverDeclaration,
    history: Sequence[CutoverTransition],
) -> CutoverState:
    """Replay a complete append-only transition history and return its state."""

    state = CutoverState.PREPARED
    previous_revision = 0
    previous_timestamp = None
    for index, transition in enumerate(history):
        if transition.previous is not state:
            raise ValueError("cutover history does not continue from its previous state")
        if transition.next not in _TRANSITIONS.get(state, set()):
            raise ValueError(f"illegal cutover transition: {state.value} -> {transition.next.value}")
        if transition.expected_activation_generation != declaration.target_generation:
            raise ValueError("cutover transition targets the wrong generation")
        if transition.cas_revision <= previous_revision:
            raise ValueError("cutover CAS revisions must increase monotonically")
        if previous_timestamp is not None and transition.occurred_at < previous_timestamp:
            raise ValueError("cutover timestamps must be monotonic")
        if (
            transition.next
            in {
                CutoverState.WRITER_FENCED,
                CutoverState.QUIESCED,
                CutoverState.MIGRATED,
                CutoverState.ACTIVATED,
                CutoverState.OBSERVED,
                CutoverState.CLEANED,
            }
            and not transition.prerequisite_evidence_digests
        ):
            raise ValueError(f"{transition.next.value} requires prerequisite evidence")
        if (
            transition.next
            in {
                CutoverState.BLOCKED_POST_FENCE,
                CutoverState.FAILED_VALIDATION,
                CutoverState.CLEANUP_BLOCKED,
            }
            and not transition.failure_reason
        ):
            raise ValueError(f"{transition.next.value} requires a failure reason")
        previous_revision = transition.cas_revision
        previous_timestamp = transition.occurred_at
        state = transition.next
        if state in _TERMINAL and index != len(history) - 1:
            raise ValueError(f"cutover history continues after terminal state {state.value}")
    return state


__all__ = ["validate_cutover_history"]
