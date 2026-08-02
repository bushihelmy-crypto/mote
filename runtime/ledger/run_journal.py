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
import math
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.runtime.ledger.append_ledger import AppendOnlyLedger, LedgerCommitReceipt
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


class RunJournalLifecycleError(RuntimeError):
    """A step attempts a non-monotonic or forked durable transition."""


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
    invocation_identity: ToolInvocationIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.started_at) not in {int, float} or not math.isfinite(self.started_at) or self.started_at < 0:
            raise UnsupportedRunJournalRecord("started_at must be a finite non-negative number")
        if self.ended_at is not None and (
            type(self.ended_at) not in {int, float} or not math.isfinite(self.ended_at) or self.ended_at < 0
        ):
            raise UnsupportedRunJournalRecord("ended_at must be a finite non-negative number or null")

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
                "invocation_identity": (
                    None if self.invocation_identity is None else self.invocation_identity.to_payload()
                ),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "StepRecord":
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
            "invocation_identity",
        }
        if set(d) != names:
            raise UnsupportedRunJournalRecord(f"[unsupported_run_journal_record] expected_fields={sorted(names)!r}")
        if type(d["step_id"]) is not str or not d["step_id"]:
            raise UnsupportedRunJournalRecord("step_id must be a non-empty string")
        if type(d["kind"]) is not str or d["kind"] not in {KIND_THINK, KIND_TOOL, KIND_TIMER}:
            raise UnsupportedRunJournalRecord("kind is not a supported run-step kind")
        if type(d["effect"]) is not str or not d["effect"]:
            raise UnsupportedRunJournalRecord("effect must be a non-empty string")
        if type(d["status"]) is not str or d["status"] not in {STARTED, COMPLETED, FAILED}:
            raise UnsupportedRunJournalRecord("status is not a supported lifecycle state")
        seq = d["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise UnsupportedRunJournalRecord("seq must be a non-negative integer")
        if type(d["name"]) is not str:
            raise UnsupportedRunJournalRecord("name must be a string")
        if d["tool_call_id"] is not None and type(d["tool_call_id"]) is not str:
            raise UnsupportedRunJournalRecord("tool_call_id must be a string or null")
        started_at = d["started_at"]
        if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
            raise UnsupportedRunJournalRecord("started_at must be a finite non-negative number")
        canonical_started_at = float(started_at)
        if not math.isfinite(canonical_started_at) or canonical_started_at < 0:
            raise UnsupportedRunJournalRecord("started_at must be a finite non-negative number")
        ended_at = d["ended_at"]
        canonical_ended_at: float | None = None
        if ended_at is not None:
            if not isinstance(ended_at, (int, float)) or isinstance(ended_at, bool):
                raise UnsupportedRunJournalRecord("ended_at must be a finite non-negative number or null")
            canonical_ended_at = float(ended_at)
            if not math.isfinite(canonical_ended_at) or canonical_ended_at < 0:
                raise UnsupportedRunJournalRecord("ended_at must be a finite non-negative number or null")
        if d["payload"] is not None and type(d["payload"]) is not str:
            raise UnsupportedRunJournalRecord("payload must be a string or null")
        if type(d["success"]) is not bool:
            raise UnsupportedRunJournalRecord("success must be a boolean")
        invocation_payload = d["invocation_identity"]
        if invocation_payload is not None and not isinstance(invocation_payload, dict):
            raise UnsupportedRunJournalRecord("invocation_identity must be an object or null")
        invocation_identity = (
            None if invocation_payload is None else ToolInvocationIdentity.from_payload(invocation_payload)
        )
        return cls(
            step_id=d["step_id"],
            kind=d["kind"],
            effect=d["effect"],
            status=d["status"],
            seq=seq,
            name=d["name"],
            tool_call_id=d["tool_call_id"],
            started_at=canonical_started_at,
            ended_at=canonical_ended_at,
            payload=d["payload"],
            success=d["success"],
            invocation_identity=invocation_identity,
        )


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

    def _parse_record(self, data: dict[str, object]) -> StepRecord:
        return StepRecord.from_dict(data)

    def _record_key(self, record: StepRecord) -> str:
        return record.step_id

    def _validate_transition(self, previous: StepRecord | None, record: StepRecord) -> None:
        if previous is None:
            if record.status != STARTED:
                raise RunJournalLifecycleError(
                    f"[run_journal_lifecycle] step={record.step_id} first_state={record.status}"
                )
            return
        if previous.status != STARTED or record.status not in {COMPLETED, FAILED}:
            raise RunJournalLifecycleError(
                f"[run_journal_lifecycle] step={record.step_id} transition={previous.status}->{record.status}"
            )
        immutable = (
            "kind",
            "effect",
            "seq",
            "name",
            "tool_call_id",
            "started_at",
            "invocation_identity",
        )
        if any(getattr(previous, field) != getattr(record, field) for field in immutable):
            raise RunJournalLifecycleError(
                f"[run_journal_lifecycle] step={record.step_id} terminal forks started identity"
            )

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
        invocation_identity: ToolInvocationIdentity | None = None,
    ) -> LedgerCommitReceipt:
        """Record that a step is about to run. For an EXTERNAL step this MUST be
        durable before the body executes, so a mid-step crash is detectable.

        ``payload`` is normally ``None`` on a started record (the result is
        carried on the terminal); a durable timer is the exception — it stamps
        its wall-clock deadline here so a resume can compute the remaining wait
        from a step that is, by design, still ``started`` while it counts down.
        """
        return self.append(
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
                invocation_identity=invocation_identity,
            )
        )

    def record_completed(
        self, step_id: str, *, payload: Optional[str] = None, success: bool = True
    ) -> LedgerCommitReceipt:
        """Record a successful terminal, carrying the result forward for healing."""
        return self._terminal(step_id, COMPLETED, payload=payload, success=success)

    def record_failed(self, step_id: str, *, payload: Optional[str] = None) -> LedgerCommitReceipt:
        """Record a failed terminal (the step ran but errored)."""
        return self._terminal(step_id, FAILED, payload=payload, success=False)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _terminal(self, step_id: str, status: str, *, payload: Optional[str], success: bool) -> LedgerCommitReceipt:
        prior = self.get(step_id)
        if prior is None:
            raise RunJournalLifecycleError(f"[run_journal_lifecycle] step={step_id} terminal_without_started")
        return self.append(
            StepRecord(
                step_id=step_id,
                kind=prior.kind,
                effect=prior.effect,
                status=status,
                seq=prior.seq,
                name=prior.name,
                tool_call_id=prior.tool_call_id,
                started_at=prior.started_at,
                ended_at=time.time(),
                payload=payload,
                success=success,
                invocation_identity=prior.invocation_identity,
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
    prior = journal.replay(step_id)
    if prior is None:
        journal.record_started(step_id, kind, effect, name=name, seq=seq, tool_call_id=tool_call_id)
    elif prior.status != STARTED:
        raise RunJournalLifecycleError(f"[run_journal_lifecycle] step={step_id} cannot_execute_from={prior.status}")
    elif prior.effect == "external":
        raise RunJournalLifecycleError(f"[run_journal_lifecycle] step={step_id} external_started_result_unknown")
    elif (prior.kind, prior.effect, prior.seq, prior.name, prior.tool_call_id) != (
        kind,
        effect,
        seq,
        name,
        tool_call_id,
    ):
        raise RunJournalLifecycleError(f"[run_journal_lifecycle] step={step_id} resumed definition mismatch")
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
    "RunJournalLifecycleError",
]
