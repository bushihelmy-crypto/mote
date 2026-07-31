"""Completion policy for the current text-output contract."""
from mote.contracts.model.turn import ToolCallAction
from mote.contracts.output.completion import CompletionDecision, CompletionKind


class TextCompletionPolicy:
    """Complete only when a channel emits one semantic final candidate."""

    async def evaluate(self, turn) -> CompletionDecision:
        candidates = turn.final_candidates
        if not candidates:
            return CompletionDecision(CompletionKind.CONTINUE)
        if len(candidates) > 1:
            return CompletionDecision(
                CompletionKind.FAIL,
                reason="multiple final candidates in one model turn",
            )
        if any(isinstance(action, ToolCallAction) for action in turn.actions):
            return CompletionDecision(
                CompletionKind.FAIL,
                reason="final candidate cannot share a turn with tool calls",
            )
        return CompletionDecision(CompletionKind.VALIDATE_CANDIDATE, candidate_index=0)
