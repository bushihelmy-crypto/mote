"""DurableBackend — Runtime seam shared by persistence tiers.

The gap it closes: a long-lived reactive loop has *replay-safe* steps whose
RESULT is worth surviving a crash even though re-running the step is itself safe
— an LLM think turn (re-running re-pays the model), a LOCAL tool write
(re-running may be expensive), a durable timer (re-running restarts the whole
wait). A durable backend memoizes each such step's result so a resume can SKIP an
already-completed step instead of blindly re-doing it.

Two implementations hang on the ONE :class:`DurableBackend` protocol so the flow
drives a single control plane and only the transport differs:

* :class:`JsonlBackend` (Tier 1, always-on, zero-dependency) — records the step's
  lifecycle in the shared :class:`~mote.runtime.ledger.RunJournal` and replays a
  completed step's recorded payload. This is NOT a deterministic replay engine:
  it only promises "skip completed steps + heal the crash frontier".
* the optional Temporal backend (Tier 2, ``runtime/durable/temporal/``) — dispatches
  ``execute`` as a Temporal activity whose result the event history memoizes.
  Its EXTERNAL-effect idempotency is still guarded by the very same ledger
  precheck inside the activity (belt-and-suspenders), so correctness never
  weakens when the backend changes.

Payloads are opaque strings here: the caller (the flow's durable runner)
owns encode/decode of a richer result (e.g. a think turn's structured output) to
and from the stored string, so this seam stays result-type-agnostic and shared
by every step kind.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from mote.runtime.ledger import COMPLETED, RunJournal, run_journaled_step


@runtime_checkable
class DurableBackend(Protocol):
    """The narrow seam the flow drives to make a step durable.

    ``run_step`` memoizes one step: if a completed record already exists (a
    resume after the step finished but before its result was consumed) its
    recorded payload is returned WITHOUT calling ``execute``; otherwise the step
    is recorded ``started``, ``execute`` runs, and a terminal record carries the
    result forward. ``step_id`` must be self-anchored to the journal (never to
    the flow's ``turn_index``, which resume does not restore).

    ``journal`` exposes the shared :class:`RunJournal` both tiers memoize into —
    the JSONL tier writes step records there directly; the Temporal tier still
    uses the same journal-backed ledger for EXTERNAL-effect idempotency inside
    its activities (belt-and-suspenders). The loop's typed façades
    (:class:`~mote.runtime.durable.think_journal.ThinkJournal`) reach the journal through
    this member, so they stay backend-agnostic.
    """

    @property
    def journal(self) -> RunJournal:
        ...

    async def run_step(
        self,
        step_id: str,
        kind: str,
        effect: str,
        execute: Callable[[], Awaitable[str]],
        *,
        name: str = "",
        seq: int = 0,
        tool_call_id: Optional[str] = None,
    ) -> str:
        ...


class JsonlBackend:
    """Tier-1 durable backend: memoize steps in the shared run journal.

    Thin façade over an injected :class:`RunJournal` (the executor owns the one
    per-session journal; the flow borrows it to build this backend, so
    ``executor`` never depends on ``flow``). Constructing it writes nothing —
    the journal only touches disk on the first record.
    """

    def __init__(self, journal: RunJournal) -> None:
        self._journal = journal

    @property
    def journal(self) -> RunJournal:
        """The shared run journal this backend memoizes steps into."""
        return self._journal

    async def run_step(
        self,
        step_id: str,
        kind: str,
        effect: str,
        execute: Callable[[], Awaitable[str]],
        *,
        name: str = "",
        seq: int = 0,
        tool_call_id: Optional[str] = None,
    ) -> str:
        """Memoize one step. Replay a completed record's payload, else run it.

        A ``completed`` record short-circuits (skip execution, reuse the recorded
        result). Any other prior state — none, ``started`` (crash mid-step), or
        ``failed`` — re-runs ``execute``: for a replay-safe step (think / LOCAL /
        timer) re-running leaves no unrecoverable external effect, so this is the
        retry path (A6 layers the retry POLICY on top). A failure records a
        terminal ``failed`` (carrying the error text) and re-raises.
        """
        prior = self._journal.replay(step_id)
        if prior is not None and prior.status == COMPLETED:
            return prior.payload or ""
        return await run_journaled_step(
            self._journal,
            step_id,
            kind,
            effect,
            execute,
            name=name,
            seq=seq,
            tool_call_id=tool_call_id,
        )


__all__ = ["DurableBackend", "JsonlBackend"]
