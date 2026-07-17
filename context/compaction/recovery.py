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
reduce, then flatten back. The bridge is *representation-based*, not
mutation-detecting: a string-bodied message round-trips losslessly through
``to_dict`` (any reducer's in-place rewrite of its content or tool-call args is
serialized faithfully), so it always re-emits via ``to_dict`` and no per-reducer
"was this touched?" check is needed. The one shape ``Message`` (whose ``content``
is a ``str``) cannot hold is a *multimodal* body — a list of content parts — so
such a dict is kept verbatim and re-emitted byte-for-byte; reducers treat those
turns as opaque (they only ever rewrite long string bodies / tool-call args).
"""

from __future__ import annotations

from typing import Optional

from mote.common.const import CACHE_INTENT, TOOL_CALL_ID, TOOL_CALLS
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
    else:
        m = Message(content=content, role=role)

    # Preserve the declarative cache-intent hint so a kept message re-emits it
    # (``to_dict`` re-serializes ``metadata[CACHE_INTENT]`` → the private wire key).
    intent = d.get("_cache_intent")
    if intent:
        m.metadata[CACHE_INTENT] = intent

    # Only a *multimodal* body (a list of content parts) cannot round-trip through
    # the str-typed ``Message.content``. Keep such a dict verbatim so a kept turn
    # is emitted byte-for-byte. Every string-bodied message round-trips losslessly
    # via ``to_dict`` — so the bridge needs no per-reducer mutation detection, and
    # any reducer that rewrites string content or tool-call args is reflected
    # automatically (see :func:`_message_to_wire`).
    if isinstance(d.get("content"), list):
        m.metadata[_WIRE_ORIGINAL] = d
    return m


def _message_to_wire(m: Message) -> dict:
    """Flatten a (possibly reduced) message back to a wire dict.

    A multimodal turn kept its pristine wire dict (a str-typed ``Message.content``
    can't hold content parts) and is re-emitted verbatim; every other message is
    serialized by ``to_dict``, which faithfully reflects any in-place reducer
    rewrite of its content or tool-call args.
    """
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
