"""DurableRunner — the loop-side think-seam façade over the durable backend.

A2 delivered the GENERIC :meth:`~mote.loop.durable.backend.JsonlBackend.run_step`
primitive; this is the FIRST typed seam façade built on top of it (A3), closing
the G1 gap: the LLM re-pay window. A think turn calls the model (expensive); if
the process crashes AFTER the model returned but BEFORE the turn's assistant
message reached the durable rollout, a naive resume re-runs the think and pays
the model again. The runner memoizes each think turn's post-dedup
:class:`~mote.common.schema.ThinkResult` in the shared run journal so a resume
can *reinstate* that result — skipping the LLM — instead of re-calling it.

Lifecycle across the loop's asynchronous think (``start`` now / ``join`` later),
so think does NOT ride ``run_step`` (whose ``await execute()`` is inline):

* :meth:`begin_think` — allocate the next self-anchored ``think:{seq}`` id and
  record it ``started`` before the model is asked.
* :meth:`complete_think` — record the post-dedup result forward once the round
  is final, so a crash before the assistant message is durable can reinstate it.
* :meth:`reap_think` — drop the record once its assistant message is durable
  (checkpoint turns) or best-effort right after recording (non-checkpoint turns,
  a rare re-pay window with no EXTERNAL side effect — the accepted Tier-1 bound).
* :meth:`reinstate_candidate` — on resume, the single completed think whose
  assistant message never reached the rebuilt history, to replay without the LLM.

Think is PURE, so this durability is a best-effort *optimization*, never a
correctness guarantee (unlike the EXTERNAL effect ledger's fsync-pre-body):
losing a think record only costs a re-pay, never a duplicated side effect.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from mote.common.const import TOOL_CALLS
from mote.common.events import emit_journal_event
from mote.common.ledger import COMPLETED, KIND_THINK, KIND_TIMER, STARTED, RunJournal
from mote.common.schema import ThinkResult, ToolEffect
from mote.loop.durable.backend import DurableBackend

if TYPE_CHECKING:
    from mote.common.schema import Message

#: Think steps are PURE (re-running only re-pays the model, no external effect).
_THINK_EFFECT = ToolEffect.PURE.value

#: A durable timer is PURE — a bounded wait has no side effect, so a dangling
#: timer left ``started`` by a crash reconciles as replay-safe (never treated as
#: an unknowable EXTERNAL effect).
_TIMER_EFFECT = ToolEffect.PURE.value


def assistant_message_present(messages: "list[Message]", result: ThinkResult) -> bool:
    """Whether *result*'s assistant turn is already durable in *messages*.

    The single matching authority shared by the resume guard (decide which
    completed thinks to reap) and the loop (decide which to reinstate), so the
    two never drift. A native turn with tool calls is matched by tool-call id
    (unique per call, robust); an XML / terminal-native turn (no calls) by exact
    assistant content. An empty, call-less result is degenerate and treated as
    already present (nothing meaningful to reinstate).
    """
    want_ids = {c.get("id") for c in (result.tool_calls or []) if c.get("id")}
    if want_ids:
        for m in messages:
            if m.is_ai_message():
                have = {c.get("id") for c in (m.metadata.get(TOOL_CALLS) or [])}
                if want_ids <= have:
                    return True
        return False
    content = result.content or ""
    if not content:
        return True
    return any(m.is_ai_message() and (m.content or "") == content for m in messages)


def reconcile_think_journal(journal: RunJournal, messages: "list[Message]") -> None:
    """Resume-time: reap think records that need no reinstatement.

    Leaves at most the single completed think whose assistant message never
    reached the rebuilt history (the loop reinstates it, skipping the LLM), and
    reaps everything else:

    * every non-completed think (``started`` / ``failed``) — its result was lost
      to the crash, so the loop must re-think fresh;
    * every completed think already durable in history — reinstating it would
      DOUBLE-record its assistant message (the guard the plan most wants tested).

    A no-op when there are no think records (a fresh or fully-reaped journal).
    """
    reap: list[str] = []
    for rec in journal.records():
        if rec.kind != KIND_THINK:
            continue
        if rec.status != COMPLETED:
            reap.append(rec.step_id)
            continue
        try:
            result = ThinkResult.model_validate_json(rec.payload or "")
        except Exception:
            reap.append(rec.step_id)  # unparseable → cannot reinstate, drop it
            continue
        if assistant_message_present(messages, result):
            reap.append(rec.step_id)
    if reap:
        journal.reap(reap)


# ---------------------------------------------------------------------------
# Durable timer (G4) — a bounded wait whose wall-clock deadline survives a crash
#
# A durable timer is the ONE step whose *result* worth surviving is carried on
# the ``started`` record, not a terminal: the record stays ``started`` for the
# whole countdown (that IS its in-flight state), stamping the wall-clock deadline
# in ``payload`` so a resume computes the remaining wait from it instead of
# restarting the countdown from zero. It is PURE — a bounded wait has no side
# effect — so a dangling timer reconciles as replay-safe, and its journaling is a
# best-effort optimization (losing the record only re-starts the wait, never
# duplicates anything). Driven by the ``wait_interruptible`` Role capability
# (which reaches ``executor.journal``), these mirror the think-seam free
# functions above rather than living on ``DurableRunner`` (whose backend the loop
# owns; the capability layer owns the wait coordination).
# ---------------------------------------------------------------------------


def begin_timer(journal: RunJournal, duration: float) -> tuple[str, float]:
    """Allocate a self-anchored ``timer:{seq}`` and record its wall-clock deadline.

    Returns ``(step_id, deadline)`` where ``deadline = now + duration``. The seq
    is recomputed from the folded journal so a timer allocated after a resume
    never collides with an already-recorded one.
    """
    seq = journal.next_timer_seq()
    step_id = journal.timer_step_id(seq)
    deadline = time.time() + duration
    journal.record_started(step_id, KIND_TIMER, _TIMER_EFFECT, seq=seq, payload=repr(deadline))
    emit_journal_event(step_id, KIND_TIMER, "started", effect=_TIMER_EFFECT, seq=seq)
    return step_id, deadline


def resume_timer(journal: RunJournal) -> Optional[tuple[str, float]]:
    """The single in-flight timer to continue waiting on, or ``None``.

    On resume a timer crashed mid-countdown is still ``started`` with its deadline
    in ``payload``; the first durable ``Sleep`` after the resume adopts it (waits
    the *remaining* time, or returns at once if the deadline has passed) instead
    of starting a fresh countdown. Returns ``(step_id, deadline)`` for the
    earliest such timer (a single-threaded loop has at most one), skipping any
    whose payload is missing or unparseable (nothing to resume from).
    """
    for rec in journal.records():
        if rec.kind != KIND_TIMER or rec.status != STARTED:
            continue
        try:
            deadline = float(rec.payload or "")
        except (TypeError, ValueError):
            continue
        return rec.step_id, deadline
    return None


def complete_timer(journal: RunJournal, step_id: str) -> None:
    """Record the timer's terminal (the wait elapsed or was woken early)."""
    journal.record_completed(step_id)
    emit_journal_event(step_id, KIND_TIMER, "completed", effect=_TIMER_EFFECT)


class DurableRunner:
    """Think-seam durable helper the loop drives over the shared run journal.

    Holds a :class:`DurableBackend` (the durable transport — the JSONL tier or
    the opt-in Temporal tier) rather than the raw journal so the later tool /
    timer seams (A4 / A5) can reach
    :meth:`~mote.loop.durable.backend.DurableBackend.run_step` on the same
    object, and stays backend-agnostic via the protocol.
    """

    def __init__(self, backend: DurableBackend) -> None:
        self._backend = backend

    @property
    def journal(self) -> RunJournal:
        """The shared run journal this runner memoizes think turns into."""
        return self._backend.journal

    def begin_think(self) -> str:
        """Allocate the next self-anchored think id and record it ``started``.

        ``seq`` is recomputed from the folded journal (``1 + max existing think
        seq``) so a fresh instance rebuilt after a crash assigns the next id
        without depending on the loop's ``turn_index`` (never restored on resume).
        """
        journal = self.journal
        seq = journal.next_think_seq()
        step_id = journal.think_step_id(seq)
        journal.record_started(step_id, KIND_THINK, _THINK_EFFECT, seq=seq)
        emit_journal_event(step_id, KIND_THINK, "started", effect=_THINK_EFFECT, seq=seq)
        return step_id

    def complete_think(self, step_id: str, result: ThinkResult) -> None:
        """Record the post-dedup *result* forward for a possible reinstate."""
        self.journal.record_completed(step_id, payload=result.model_dump_json())
        emit_journal_event(step_id, KIND_THINK, "completed", effect=_THINK_EFFECT)

    def fail_think(self, step_id: str) -> None:
        """Record a think that ultimately failed, then reap it in-process.

        The think-seam's failure twin of :meth:`complete_think`. A think call
        that exhausts the LLM layer's own retry/recovery (``base_llm``'s
        :class:`RecoveryRunner` — A6 adds NO new retry engine) surfaces as an
        exception out of the loop's act step; this records a ``failed`` terminal
        for the round's step so the crash-resume guard treats it as a lost round
        (re-think fresh, never reinstate a half-result), then reaps it right away.

        Reaping in-process is the boundedness half of A6: without it a failed
        think's ``started`` record would linger until a *resume* ran
        :func:`reconcile_think_journal`; a long-lived process that never resumes
        would accumulate one dangling record per ultimately-failed think. Think
        is PURE, so dropping the record is safe — it can only cost a re-pay, never
        a duplicated side effect.
        """
        self.journal.record_failed(step_id)
        emit_journal_event(step_id, KIND_THINK, "failed", effect=_THINK_EFFECT)
        self.journal.reap([step_id])
        emit_journal_event(step_id, KIND_THINK, "reaped", effect=_THINK_EFFECT)

    def reap_think(self, step_id: str) -> None:
        """Drop a resolved think record (its assistant message is durable)."""
        self.journal.reap([step_id])
        emit_journal_event(step_id, KIND_THINK, "reaped", effect=_THINK_EFFECT)

    def reinstate_candidate(self, messages: "list[Message]") -> Optional[tuple[str, ThinkResult]]:
        """The completed think to replay without the LLM, or ``None``.

        Returns ``(step_id, result)`` for a completed think whose assistant
        message is NOT yet in *messages* — a crash between the model returning
        and its assistant message reaching the rollout. History-matched every
        call (not merely trusting the resume guard's reap) so a stale completed
        record whose turn actually finished is never wrongly reinstated.
        """
        for rec in self.journal.records():
            if rec.kind != KIND_THINK or rec.status != COMPLETED:
                continue
            try:
                result = ThinkResult.model_validate_json(rec.payload or "")
            except Exception:
                continue
            if not assistant_message_present(messages, result):
                return rec.step_id, result
        return None


__all__ = [
    "DurableRunner",
    "assistant_message_present",
    "reconcile_think_journal",
    "begin_timer",
    "resume_timer",
    "complete_timer",
]
