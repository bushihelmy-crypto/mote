#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LoopGuardSubscriber — the loop guard as a PostToolUse control subscriber.

This puts the repeated-failure / no-progress guard *on* the control plane, a
peer of the permission gate and the secret-redaction rewriter, rather than
wedging a bespoke controller into ``run_command``. The whole feature is one
PostToolUse subscriber: it folds a :class:`~mote.executor.loop_guard.detector.Verdict`
from the finished call into an in-band nudge appended to that call's result.

Two properties place it precisely on the plane:

* ``handles = (POST_TOOL_USE,)`` routes only finished-call events here.
* ``stage = ControlStage.GATE`` runs it **after** the redaction rewriter
  (``ControlStage.REWRITE``) in the shared PostToolUse bucket, so the nudge is
  appended to the already-redacted output (never before, which would let a
  rewrite clobber it).

``fail_mode`` stays the default ``FAIL_OPEN``: this is an *advisory* nudge, not a
security gate, so a crash in the guard must let the call through untouched, never
brick the turn.

The soft-signal design is deliberate. The control plane's PostToolUse outcome
can append context to a result but cannot synthesize a different tool call, so
this guard does not *force* an ``AskUserQuestion`` (that lives in the think-layer
``check_duplicate_calls``). Instead it appends a nudge — the concrete failure/
no-progress fact plus a steer to change approach or ask the user — into the
result the model reads next turn, leaving the decision to the model. It reuses
:class:`ToolResultOutcome.additional_context`, the field built exactly for
"append to a tool result", so no new plumbing is introduced.

Like the permission gate, this module never imports a tool: the executor injects
a ``resolve_readonly`` closure (does this tool name resolve to a PURE tool?) and
a ``sig_of`` closure (the stable args signature), so the subscriber — and the
bus beneath it — stays tool-free.
"""
from __future__ import annotations

from typing import Callable, Optional

from mote.common.events.outcomes import ToolResultOutcome
from mote.common.events.types import POST_TOOL_USE, PostToolUseEvent
from mote.common.interface.event_subscriber import ControlStage, ControlSubscriber
from mote.common.text.hashing import content_hash
from mote.executor.loop_guard.detector import ThrashDetector, Verdict


def _nudge(verdict: Verdict) -> str:
    """Render the in-band steer appended to a thrashing call's result.

    Names the concrete streak (so the model sees "3 times", not "repeatedly") and
    points at the two productive exits: change approach, or ask the user via
    ``AskUserQuestion``. Kept terse — it rides on top of a real tool result.
    """
    if verdict.kind == "repeated_failure":
        what = (
            f"[loop guard] '{verdict.tool_name}' has now failed {verdict.count} times in a row "
            f"with the same arguments."
        )
    else:
        what = (
            f"[loop guard] '{verdict.tool_name}' has returned the same result {verdict.count} times "
            f"in a row — this read is not making progress."
        )
    return (
        f"{what} Stop reissuing it unchanged: change your approach, or if you are blocked, "
        f"use AskUserQuestion to ask the user for guidance."
    )


class LoopGuardSubscriber(ControlSubscriber):
    """Appends a thrash nudge to a finished tool's result (fail-open, advisory)."""

    #: Only finished-call events reach this subscriber (bus routing key).
    handles: tuple[str, ...] = (POST_TOOL_USE,)
    #: Run after the redaction REWRITE so the nudge lands on the final output.
    stage: ControlStage = ControlStage.GATE
    #: Provenance label the bus stamps onto the context this subscriber folds.
    name: str = "loop-guard"

    def __init__(
        self,
        detector: ThrashDetector,
        resolve_readonly: Callable[[str], bool],
        sig_of: Callable[[str, dict], str],
    ) -> None:
        """Args:
        detector: The per-Role streak state machine.
        resolve_readonly: ``name -> is this a PURE (read-only) tool?`` — an
            executor-supplied closure so the guard reads tool effect without
            importing a tool. Unknown names resolve ``False`` (not read-only), so
            they are never eligible for no-progress and only ever count failures.
        sig_of: ``(name, args) -> stable order-insensitive signature`` — the
            executor supplies the one signature policy so the guard holds no
            serialization logic of its own.
        """
        self._detector = detector
        self._resolve_readonly = resolve_readonly
        self._sig_of = sig_of

    async def handle_control(self, event) -> Optional[ToolResultOutcome]:
        # Only tool-finished events are ours; anything else is not.
        if not isinstance(event, PostToolUseEvent):
            return None

        name = event.tool_name
        args = event.tool_input if isinstance(event.tool_input, dict) else {}
        sig = self._sig_of(name, args)
        is_readonly = self._resolve_readonly(name)
        # A stable fingerprint of the result text, consulted ONLY on the PURE-tool
        # success path (no-progress detection). Hash lazily on that path alone so a
        # failed call, a non-PURE call, or any large-payload read that will never
        # be compared does not pay a SHA-256 over up to ~100k chars on the
        # per-call hot path. The detector ignores the fingerprint in every other
        # branch, so the empty default is behavior-identical.
        if is_readonly and event.success:
            response = event.tool_response if isinstance(event.tool_response, str) else ""
            fingerprint = content_hash(response)
        else:
            fingerprint = ""

        verdict = self._detector.record(
            tool_name=name,
            sig=sig,
            success=event.success,
            is_readonly=is_readonly,
            result_fingerprint=fingerprint,
        )
        if verdict is None:
            return None
        return ToolResultOutcome(additional_context=[_nudge(verdict)])


__all__ = ["LoopGuardSubscriber"]
