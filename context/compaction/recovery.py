"""Reactive (HARD) context reduction for the LLM recovery loop.

``BaseLLM``'s recovery loop, on a context-overflow error that survives the
transient-retry budget, needs to shrink the *outgoing wire payload* and re-issue
it. This adapter is the concrete :class:`ContextReducer` the upper layer injects
for that: it runs the same boundary-safe machinery the normal path uses,
escalating fold → summarize → drop and stopping as soon as the target is met.

On overflow we preserve as much history as possible: summarize (LLM-condense the
head, keep the tail) is far less lossy than the destructive head-drop, so it runs
*before* drop, with drop kept as the guaranteed floor when summarize can't free
enough (or is unavailable). Summarize fires *inside* an in-flight LLM call and
issues its own inner aask(), but that runs on the router's dedicated COMPRESSION
instance, built reducer-less (``context_reducer=None``; see
``LLMRouter._build`` ``LLMVariant.COMPRESSION``) — so the inner call cannot re-enter
``_compress``. The fold→summarize→drop cycle is broken at the injection layer, no
runtime guard required. Fold (free, in-place) and head-drop (drop oldest whole
turns) shrink the payload deterministically without a model round-trip.

The wire<->Message bridge: ``BaseLLM`` holds messages as ``Message.to_dict()``
wire dicts. We reconstruct :class:`Message` objects (restoring the
``tool_calls`` / ``tool_call_id`` metadata the :class:`Transcript` groups on),
reduce, then flatten back. Kept non-tool-result messages are emitted from their
pristine original dict (so multimodal image parts are never lossily flattened);
tool-result messages round-trip through ``to_dict`` so an in-place fold is
reflected.
"""

from __future__ import annotations

from typing import Optional

from mote.common.const import TOOL_CALL_ID, TOOL_CALLS
from mote.common.schema import Message
from mote.context.compaction.pipeline import ReductionPipeline
from mote.context.compaction.request import ReductionReason, ReductionRequest, Urgency
from mote.context.compaction.transcript import Transcript

# Private metadata key stashing the original wire dict on a reconstructed
# message, so a kept (undropped, unfolded) message is emitted byte-for-byte.
_WIRE_ORIGINAL = "_recovery_wire_original"


def _flatten_content(content) -> str:
    """Best-effort string view of a wire ``content`` for token counting."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if content is None else str(content)


def _wire_to_message(d: dict) -> Message:
    """Reconstruct a :class:`Message` (with tool metadata) from a wire dict."""
    role = d.get("role", "user")
    content = _flatten_content(d.get("content"))

    tool_call_id = d.get("tool_call_id")
    if tool_call_id:
        m = Message(content=content, role="tool")
        m.metadata[TOOL_CALL_ID] = tool_call_id
        return m

    tool_calls = d.get("tool_calls")
    if tool_calls:
        parsed = [
            {
                "id": c.get("id", ""),
                "name": (c.get("function") or {}).get("name", ""),
                "args": (c.get("function") or {}).get("arguments", ""),
            }
            for c in tool_calls
        ]
        m = Message(content=content, role=role or "assistant")
        m.metadata[TOOL_CALLS] = parsed
        m.metadata[_WIRE_ORIGINAL] = d
        return m

    m = Message(content=content, role=role)
    m.metadata[_WIRE_ORIGINAL] = d
    return m


def _message_to_wire(m: Message) -> dict:
    """Flatten a (possibly reduced) message back to a wire dict."""
    # A tool-result body may have been folded in place → reflect current content.
    if m.metadata.get(TOOL_CALL_ID):
        return m.to_dict()
    original = m.metadata.get(_WIRE_ORIGINAL)
    if original is not None:
        return original
    return m.to_dict()


class RecoveryContextReducer:
    """A :class:`ContextReducer` that runs a HARD fold+drop pipeline on wire dicts."""

    def __init__(self, reducers, *, model: str = "gpt-4") -> None:
        # Cheapest-first ordering + the stop-when-target-met policy are the
        # pipeline's job; we just hand it the non-LLM reducers.
        self._pipeline = ReductionPipeline(reducers, model=model)

    async def reduce(self, messages: list[dict], *, target_tokens: int) -> Optional[list[dict]]:
        if not messages:
            return None
        transcript = Transcript.from_messages([_wire_to_message(d) for d in messages])
        request = ReductionRequest(
            target_tokens=target_tokens,
            urgency=Urgency.HARD,
            reason=ReductionReason.REACTIVE,
        )
        outcome = await self._pipeline.run(transcript, request)
        if not outcome.changed:
            return None
        return [_message_to_wire(m) for m in outcome.transcript.to_messages()]
