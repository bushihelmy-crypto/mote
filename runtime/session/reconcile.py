"""Resume reconciliation — heal dangling tool calls after a crash-resume.

The gap it closes: :func:`replay` rebuilds history from the rollout, but a crash
mid-turn can leave an assistant ``tool_calls`` message on disk with NO paired
``tool_result`` for one or more of its calls (the results were never flushed).
That broken pairing 400s the very next provider request. The
:class:`~mote.runtime.tools.effect_ledger.EffectLedger` remembers what actually ran,
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

Layering: this module reads the ledger through the narrow :class:`LedgerView`
structural protocol (``status`` only) so ``session`` keeps its no-dependency on
``executor``. The reconciler is pure — it never mutates the ledger; it returns
the set of resolved call ids for the caller to :meth:`reap`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Set, runtime_checkable

from mote.contracts.constants.messages import TOOL_CALL_ID, TOOL_CALLS
from mote.contracts.schema import Message, ToolMessage

#: The one ledger state that means "in flight when the crash hit"; mirrors the
#: stable wire value in :mod:`mote.runtime.tools.effect_ledger` (kept local so the
#: ``session`` package needs no import of ``executor``). Any other non-None
#: status is terminal (``completed``/``failed``) and carries a result to heal.
_STARTED = "started"

#: The replay-safe side-effect classes (mirrors ``ToolEffect.PURE``/``LOCAL``
#: values, kept local so ``session`` needs no import of ``common.schema``). A
#: dangling ``started`` call of one of these left no unrecoverable external
#: effect, so it is safe to re-run; any other effect (EXTERNAL, or an unknown/
#: missing value — fail-closed) is guarded against blind replay.
_REPLAY_SAFE_EFFECTS = frozenset({"pure", "local"})

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
class EffectRecordView(Protocol):
    """The slice of a ledger record the reconciler reads (structural).

    Declared as read-only properties so a frozen ``EffectRecord`` (whose fields
    are immutable) satisfies the protocol — a read-write attribute declaration
    would reject the frozen dataclass on variance grounds.
    """

    @property
    def status(self) -> str:
        ...

    @property
    def result(self) -> Optional[str]:
        ...

    @property
    def effect(self) -> str:
        ...


@runtime_checkable
class LedgerView(Protocol):
    """The slice of :class:`EffectLedger` the reconciler needs — a read of one
    call's latest record. Duck-typed so ``session`` stays free of ``executor``."""

    def status(self, tool_call_id: str) -> Optional[EffectRecordView]:
        ...


@dataclass
class ReconcileResult:
    """Outcome of a resume reconciliation pass.

    ``messages`` is the rebuilt history with synthetic results spliced in;
    ``resolved_ids`` are the ledger call ids the caller should :meth:`reap` —
    only those already paired in the rollout (their result is durable, so the
    record is stale). A just-healed dangling call is NOT included: its record
    must survive to re-heal it on a later resume (see :func:`reconcile_tool_calls`).
    The counts are for logging only.
    """

    messages: List[Message]
    resolved_ids: Set[str] = field(default_factory=set)
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


def _is_replay_safe(record: EffectRecordView) -> bool:
    """Whether a dangling ``started`` *record* is safe to re-run.

    True only for a recorded PURE/LOCAL effect (re-running leaves no
    unrecoverable external side effect). Fail-closed: an EXTERNAL, unknown, or
    missing effect is treated as unsafe — never blindly replayed.
    """
    return getattr(record, "effect", "external") in _REPLAY_SAFE_EFFECTS


def _classify(record: Optional[EffectRecordView]) -> str:
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
    if record.status == _STARTED:
        return "replay" if _is_replay_safe(record) else "unknown"
    return "heal"


def _synthetic_result(call: dict, record: Optional[EffectRecordView]) -> ToolMessage:
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
        content = (record.result if record is not None else "") or ""
    return ToolMessage(content=content, tool_call_id=call_id)


def reconcile_tool_calls(messages: List[Message], ledger: LedgerView) -> ReconcileResult:
    """Repair dangling tool calls in replayed *history* using the *ledger*.

    A call is *dangling* when its id appears in an assistant ``tool_calls`` block
    but no ``tool_result`` for that id is present in *messages* (its result was
    lost to a crash before it was flushed). For each dangling call a synthetic
    result is spliced right after its owning assistant message; the content is
    chosen from the ledger (heal / unknown-after-crash / safe-retry). Non-dangling
    calls are untouched.

    Pure: reads the ledger only via :meth:`status`. Returns the rebuilt history
    plus the ids the caller should reap — ONLY the calls whose result is already
    a durable part of the rollout (paired in *messages*) and whose ledger record
    is therefore stale. A just-healed dangling call is deliberately NOT reaped:
    its synthetic result lives only in the in-memory history, so the record must
    survive to heal the same dangling call again on a second resume. Reaping it
    early would drop the record, and a later resume — replaying the still-dangling
    call from the rollout with an empty ledger — would treat it as a safe replay
    and re-run the (possibly EXTERNAL) effect. It is reaped only once its real
    result is durably recorded (becoming a paired/stale record on a later pass).
    """
    # Which call ids already have a paired result in the replayed history.
    resolved: Set[str] = set()
    for m in messages:
        meta = getattr(m, "metadata", None) or {}
        cid = meta.get(TOOL_CALL_ID)
        if cid:
            resolved.add(cid)

    result = ReconcileResult(messages=[])
    reaped: Set[str] = set()

    for m in messages:
        result.messages.append(m)
        calls = _iter_calls(m)
        if not calls:
            continue
        for call in calls:
            cid = call.get("id")
            if not cid:
                continue
            record = ledger.status(cid)
            if cid in resolved:
                # Already paired IN THE ROLLOUT: its result is a durable part of
                # the truth source, so any ledger record for it is genuinely stale
                # -> safe to reap. (Nothing to inject; the pairing already holds.)
                if record is not None:
                    reaped.add(cid)
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

    result.resolved_ids = reaped
    return result


__all__ = ["reconcile_tool_calls", "ReconcileResult", "LedgerView", "EffectRecordView"]
