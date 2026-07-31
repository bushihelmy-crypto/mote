"""Crash-durable journal for think, tool, and timer run steps.

Step identity is *self-anchored to the journal itself* — never to the loop's
``turn_index``, which resume does NOT restore (so any turn-derived id would
collide with a committed round). A ``tool``/``timer`` step keys off its stable
``tool_call_id``; a ``think`` step keys off ``think:{seq}`` where ``seq`` is
``1 + max(existing think seq)`` recomputed purely from the folded journal, so a
fresh instance rebuilt in a new process assigns the next id without clashing with
any already-recorded think step.

Lifecycle mirrors the effect ledger: ``record_started`` (durable BEFORE the step
runs, for EXTERNAL) → ``record_completed`` / ``record_failed`` (carrying the
result forward so a resume can heal a dangling step). The append/fold/rewrite/
crash-durability mechanics all live in the shared
:class:`~mote.runtime.ledger.AppendOnlyLedger` base.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from mote.runtime.ledger import AppendOnlyLedger
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace

#: Filename of the append-only journal inside a session's ``ledger/`` space.
#: Current canonical filename inside a session's ``ledger/`` space.
JOURNAL_FILE_NAME = "run-journal.jsonl"

#: The step kinds a journal record can carry.
KIND_THINK = "think"
KIND_TOOL = "tool"
KIND_TIMER = "timer"

#: The three lifecycle states a step record can carry (wire-stable strings,
#: canonical wire values.
STARTED = "started"
COMPLETED = "completed"
FAILED = "failed"


class UnsupportedRunJournalRecord(RuntimeError):
    """A durable journal line does not use the current StepRecord schema."""


@dataclass(frozen=True)
class StepRecord:
    """One folded journal entry for a single run step (think / tool / timer).

    ``payload`` holds the step's forward-carried result for a terminal record —
    a think step's post-dedup output, a tool step's final (post-size-limit)
    output, or a timer's wall-clock deadline — so a resume can reuse it without
    re-running the step. ``started`` records leave it ``None``.

    For a tool step, ``step_id`` is its stable ``tool_call_id``.
    """

    step_id: str
    kind: str
    effect: str
    status: str
    seq: int = 0
    name: str = ""
    tool_call_id: Optional[str] = None
    started_at: float = 0.0
    ended_at: Optional[float] = None
    payload: Optional[str] = None
    success: bool = True

    def to_json(self) -> str:
        return json.dumps(
            {
                "step_id": self.step_id,
                "kind": self.kind,
                "effect": self.effect,
                "status": self.status,
                "seq": self.seq,
                "name": self.name,
                "tool_call_id": self.tool_call_id,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "payload": self.payload,
                "success": self.success,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "StepRecord":
        names = {
            "step_id",
            "kind",
            "effect",
            "status",
            "seq",
            "name",
            "tool_call_id",
            "started_at",
            "ended_at",
            "payload",
            "success",
        }
        if set(d) != names:
            raise UnsupportedRunJournalRecord(f"[unsupported_run_journal_record] expected_fields={sorted(names)!r}")
        return cls(**d)


class RunJournal(AppendOnlyLedger[StepRecord]):
    """Durable started/completed/failed journal for one session's run steps.

    Cheap to construct: the base folds any existing on-disk log into an in-memory
    latest-per-``step_id`` index, so a fresh instance for the same ``session_id``
    — e.g. one rebuilt by a resume in a new process — sees the pre-crash state.
    """

    def __init__(self, session_id: str, store: SessionWorkspace | None = None) -> None:
        self._session_id = session_id
        self._store = store or SessionWorkspace()
        path = self._store.space(session_id, SessionSpace.LEDGER) / JOURNAL_FILE_NAME
        super().__init__(path)

    @property
    def session_id(self) -> str:
        """The session this journal belongs to (its ``ledger/`` space owner)."""
        return self._session_id

    @property
    def store(self) -> SessionWorkspace:
        """The workspace store that resolves this journal's on-disk location."""
        return self._store

    # ------------------------------------------------------------------
    # Base hooks
    # ------------------------------------------------------------------

    def _parse_record(self, data: dict) -> StepRecord:
        return StepRecord.from_dict(data)

    def _record_key(self, record: StepRecord) -> str:
        return record.step_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def replay(self, step_id: str) -> Optional[StepRecord]:
        """The latest record for *step_id*, or ``None`` if never recorded."""
        return self.get(step_id)

    def unresolved(self) -> list[StepRecord]:
        """Every step whose latest state is ``started`` — unknown after a crash."""
        return [r for r in self.records() if r.status == STARTED]

    def next_think_seq(self) -> int:
        """The next self-anchored think ``seq`` (``1 + max existing think seq``).

        Recomputed purely from the folded journal so a fresh instance rebuilt
        after a crash assigns the next id without depending on the loop's
        ``turn_index`` (which resume never restores).
        """
        return 1 + max((r.seq for r in self.records() if r.kind == KIND_THINK), default=0)

    def think_step_id(self, seq: int) -> str:
        """The stable step id for a think step at *seq* (``think:{seq}``)."""
        return f"{KIND_THINK}:{seq}"

    def next_timer_seq(self) -> int:
        """The next self-anchored timer ``seq`` (``1 + max existing timer seq``).

        Recomputed purely from the folded journal — like :meth:`next_think_seq` —
        so a durable timer allocated after a crash-resume never collides with an
        already-recorded one (``turn_index`` is not restored on resume).
        """
        return 1 + max((r.seq for r in self.records() if r.kind == KIND_TIMER), default=0)

    def timer_step_id(self, seq: int) -> str:
        """The stable step id for a timer step at *seq* (``timer:{seq}``)."""
        return f"{KIND_TIMER}:{seq}"

    # ------------------------------------------------------------------
    # Writes (each appends one durable line and updates the in-memory index)
    # ------------------------------------------------------------------

    def record_started(
        self,
        step_id: str,
        kind: str,
        effect: str,
        *,
        name: str = "",
        seq: int = 0,
        tool_call_id: Optional[str] = None,
        payload: Optional[str] = None,
    ) -> None:
        """Record that a step is about to run. For an EXTERNAL step this MUST be
        durable before the body executes, so a mid-step crash is detectable.

        ``payload`` is normally ``None`` on a started record (the result is
        carried on the terminal); a durable timer is the exception — it stamps
        its wall-clock deadline here so a resume can compute the remaining wait
        from a step that is, by design, still ``started`` while it counts down.
        """
        self.append(
            StepRecord(
                step_id=step_id,
                kind=kind,
                effect=effect,
                status=STARTED,
                seq=seq,
                name=name,
                tool_call_id=tool_call_id,
                started_at=time.time(),
                payload=payload,
            )
        )

    def record_completed(self, step_id: str, *, payload: Optional[str] = None, success: bool = True) -> None:
        """Record a successful terminal, carrying the result forward for healing."""
        self._terminal(step_id, COMPLETED, payload=payload, success=success)

    def record_failed(self, step_id: str, *, payload: Optional[str] = None) -> None:
        """Record a failed terminal (the step ran but errored)."""
        self._terminal(step_id, FAILED, payload=payload, success=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _terminal(self, step_id: str, status: str, *, payload: Optional[str], success: bool) -> None:
        prior = self.get(step_id)
        self.append(
            StepRecord(
                step_id=step_id,
                kind=prior.kind if prior is not None else KIND_TOOL,
                effect=prior.effect if prior is not None else "external",
                status=status,
                seq=prior.seq if prior is not None else 0,
                name=prior.name if prior is not None else "",
                tool_call_id=prior.tool_call_id if prior is not None else None,
                started_at=prior.started_at if prior is not None else time.time(),
                ended_at=time.time(),
                payload=payload,
                success=success,
            )
        )


async def run_journaled_step(
    journal: RunJournal,
    step_id: str,
    kind: str,
    effect: str,
    execute: Callable[[], Awaitable[str]],
    *,
    name: str = "",
    seq: int = 0,
    tool_call_id: Optional[str] = None,
) -> str:
    """The ONE side-effecting durable-step body both persistence tiers share.

    Records ``started`` → runs ``execute`` → records the terminal
    (``completed`` with the payload, or ``failed`` with the error text then
    re-raises). This is the correctness-critical sequence whose drift would
    cause a double side effect, so both :class:`~mote.runtime.durable.JsonlBackend`
    and the Temporal tier's activity delegate here to stay identical BY
    CONSTRUCTION rather than by a hand-maintained "mirrors exactly" comment.

    The caller owns the ``completed``-record short-circuit (a pure idempotent
    ``replay`` read) so it can sit AHEAD of any caller-specific step resolution
    — e.g. the Temporal activity must serve a completed step from the journal
    even when its process-local handler was never re-registered, so it cannot
    fold the short-circuit into this helper. This helper is entered only once
    the caller has decided the step must actually run.
    """
    journal.record_started(step_id, kind, effect, name=name, seq=seq, tool_call_id=tool_call_id)
    try:
        payload = await execute()
    except Exception as exc:
        journal.record_failed(step_id, payload=str(exc))
        raise
    journal.record_completed(step_id, payload=payload)
    return payload


__all__ = [
    "RunJournal",
    "run_journaled_step",
    "StepRecord",
    "STARTED",
    "COMPLETED",
    "FAILED",
    "KIND_THINK",
    "KIND_TOOL",
    "KIND_TIMER",
    "JOURNAL_FILE_NAME",
    "UnsupportedRunJournalRecord",
]
