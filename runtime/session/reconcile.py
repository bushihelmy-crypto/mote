"""Resume reconciliation — heal dangling tool calls after a crash-resume.

The gap it closes: :func:`replay` rebuilds history from the rollout, but a crash
mid-turn can leave an assistant ``tool_calls`` message on disk with NO paired
``tool_result`` for one or more of its calls (the results were never flushed).
That broken pairing 400s the very next provider request. The
The Tool effect store remembers what actually ran,
so this reconciler is the bridge: for every *dangling* call (requested but with
no result in the replayed history) it injects a synthetic ``tool_result`` right
after its owning assistant message, choosing the content from the ledger:

  * terminal record (``completed``/``failed``) -> **heal**: emit the recorded
    result verbatim, so the effect is NOT re-run (its result message was simply
    lost to the crash).
  * ``started`` + EXTERNAL (or unknown/missing effect — fail-closed) -> the call
    was in flight when the crash hit and its external outcome is physically
    unknowable to the framework, so re-running it risks a duplicate side effect:
    emit ``<unknown-after-crash>`` and leave the decision (verify / retry /
    abandon) to the model — the framework never guesses.
  * ``started`` + PURE/LOCAL -> a replay-safe call left no unrecoverable
    external effect, so re-running it is safe -> emit a retry note.
  * no record at all -> the call was never ledgered or never started: safe to
    replay -> emit a note inviting a retry.

Injecting a result for every dangling call restores the provider pairing
invariant unconditionally; the content only differs in what it tells the model.

Layering: this module reads the Tool effect owner through a narrow query
protocol. The reconciler is a pure projection and never mutates durable facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable

from mote.contracts.conversation import Message, ToolMessage
from mote.contracts.conversation.fields import TOOL_CALL_ID, TOOL_CALLS
from mote.contracts.tool.effects import ToolEffect
from mote.runtime.tools.effect_store import ToolEffectState

#: The one ledger state that means "in flight when the crash hit"; mirrors the
_UNKNOWN_AFTER_CRASH = (
    "<unknown-after-crash>\n"
    "Tool '{name}' (call {call_id}) was started before a restart but its outcome "
    "was never recorded, so re-running it could duplicate an external side effect. "
    "It was NOT re-run. Verify whether the effect already took hold; reissue the "
    "call only if it is safe to retry."
    "\n</unknown-after-crash>"
)

_SAFE_RETRY = (
    "<not-executed>\n"
    "Tool '{name}' (call {call_id}) did not complete before a restart and left no "
    "external side effect on record (it is replay-safe), so nothing happened. "
    "Reissue the call if you still need its result."
    "\n</not-executed>"
)


@runtime_checkable
class ToolEffectRecordView(Protocol):
    """The slice of a ledger record the reconciler reads (structural).

    Declared as read-only properties so an immutable domain record satisfies
    the protocol — a read-write attribute declaration
    would reject the frozen dataclass on variance grounds.
    """

    @property
    def state(self) -> ToolEffectState: ...

    @property
    def receipt(self) -> Optional[str]: ...

    @property
    def capability(self) -> ToolEffect: ...


@runtime_checkable
class ToolEffectQuery(Protocol):
    """The Tool effect store slice needed to read one call's latest record."""

    def lookup(self, invocation_id: str) -> Optional[ToolEffectRecordView]: ...


@dataclass
class ReconcileResult:
    """Outcome of a resume reconciliation pass.

    ``messages`` is the rebuilt history with synthetic results spliced in;
    Tool effect facts are retained by their canonical owner; reconciliation is
    a read-only projection and never decides retention or deletion.
    """

    messages: List[Message]
    healed: int = 0
    flagged: int = 0
    replayable: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.healed or self.flagged or self.replayable)


def _iter_calls(message: Message) -> List[dict]:
    """The tool calls an assistant message requested (``[]`` if none)."""
    meta = getattr(message, "metadata", None) or {}
    calls = meta.get(TOOL_CALLS)
    return calls if isinstance(calls, list) else []


def _is_replay_safe(record: ToolEffectRecordView) -> bool:
    """Whether a dangling ``started`` *record* is safe to re-run.

    True only for a recorded PURE/LOCAL effect (re-running leaves no
    unrecoverable external side effect). Fail-closed: an EXTERNAL, unknown, or
    missing effect is treated as unsafe — never blindly replayed.
    """
    return record.capability in {ToolEffect.PURE, ToolEffect.LOCAL}


def _classify(record: Optional[ToolEffectRecordView]) -> str:
    """The dangling-call outcome: ``"replay"`` / ``"unknown"`` / ``"heal"``.

    - no record → ``replay`` (never ledgered / never started: safe to reissue).
    - ``started`` + replay-safe effect → ``replay`` (PURE/LOCAL: nothing external
      happened, re-running is safe).
    - ``started`` + EXTERNAL/unknown effect → ``unknown`` (in-flight outcome is
      physically unknowable; never silently re-run an external effect).
    - terminal (``completed``/``failed``) → ``heal`` (its result was simply lost
      to the crash; emit the recorded result verbatim, do NOT re-run).
    """
    if record is None:
        return "replay"
    if record.state is ToolEffectState.INTENT_COMMITTED:
        return "replay" if _is_replay_safe(record) else "unknown"
    return "heal"


def _synthetic_result(call: dict, record: Optional[ToolEffectRecordView]) -> ToolMessage:
    """Build the tool_result to pair a dangling *call*, per its ledger *record*."""
    call_id = call.get("id", "")
    name = call.get("name", "?")
    verdict = _classify(record)
    if verdict == "replay":
        content = _SAFE_RETRY.format(name=name, call_id=call_id)
    elif verdict == "unknown":
        content = _UNKNOWN_AFTER_CRASH.format(name=name, call_id=call_id)
    else:
        # Terminal record (completed/failed): heal from the stored result.
        content = (record.receipt if record is not None else "") or ""
    return ToolMessage(content=content, tool_call_id=call_id)


def reconcile_tool_calls(messages: List[Message], ledger: ToolEffectQuery) -> ReconcileResult:
    """Repair dangling tool calls in replayed *history* using the *ledger*.

    A call is *dangling* when its id appears in an assistant ``tool_calls`` block
    but no ``tool_result`` for that id is present in *messages* (its result was
    lost to a crash before it was flushed). For each dangling call a synthetic
    result is spliced right after its owning assistant message; the content is
    chosen from the ledger (heal / unknown-after-crash / safe-retry). Non-dangling
    calls are untouched.

    Pure: reads the ledger only via :meth:`status`. Returns the rebuilt history
    with counters for observability. Tool effect retention is deliberately not
    part of resume projection; only the canonical owner may delete those facts.
    """
    # Which call ids already have a paired result in the replayed history.
    resolved: set[str] = set()
    for m in messages:
        meta = getattr(m, "metadata", None) or {}
        cid = meta.get(TOOL_CALL_ID)
        if cid:
            resolved.add(cid)

    result = ReconcileResult(messages=[])

    for m in messages:
        result.messages.append(m)
        calls = _iter_calls(m)
        if not calls:
            continue
        for call in calls:
            cid = call.get("id")
            if not cid:
                continue
            record = ledger.lookup(cid)
            if cid in resolved:
                # Already paired in the rollout: nothing to project. Retention
                # remains the Tool effect owner's responsibility.
                continue
            # Dangling: its result was lost to the crash and is NOT in the rollout.
            # Splice a synthetic result into the IN-MEMORY history only — the
            # rollout still lacks it — so we must NOT reap this record: a second
            # resume replays the same dangling call from the rollout and needs the
            # record to heal it again (reconcile is idempotent). The record is
            # reaped later, once its real result is durably recorded (a re-run) or
            # never (a healed EXTERNAL call the model chose not to reissue), at
            # which point it becomes a paired/stale record handled above.
            result.messages.append(_synthetic_result(call, record))
            verdict = _classify(record)
            if verdict == "replay":
                result.replayable += 1
            elif verdict == "unknown":
                result.flagged += 1
            else:
                result.healed += 1

    return result


__all__ = ["reconcile_tool_calls", "ReconcileResult", "ToolEffectQuery", "ToolEffectRecordView"]
