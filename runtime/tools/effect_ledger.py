"""EffectLedger — the EXTERNAL-tool-effect view over the run journal.

The gap it closes: a tool whose effect escapes the locally-recoverable boundary
(network / IPC / subprocess / a human-visible action / a spawned agent) has no
before-image snapshot, so if the process crashes *after* the effect happened but
*before* the tool-result message is durably flushed, a naive resume would replay
the call and duplicate the effect. This ledger is the missing bookkeeping: a
tiny append-only record per ``(session_id, tool_call_id)`` that survives a crash
and lets the resume reconciler tell an in-flight call from a finished one.

``ToolEffect.EXTERNAL`` and ``ToolEffect.LOCAL`` calls are ledgered; PURE reads
are cheap to re-derive so they are never recorded (see
:meth:`BaseTool.resolve_effect`). The two recorded classes differ in how a
resume treats an *in-flight* (``started``) record — the ``effect`` field carried
on each record is what lets the reconciler tell them apart: an EXTERNAL call
left in flight is refused as unknown-after-crash (its side effect is unknowable),
whereas a LOCAL one is replay-safe and simply re-run. A ``completed`` record of
either class is healed by reusing its stored result.

Lifecycle of one call, written at the ``ToolExecutor.run_command`` chokepoint:

    mark_started(id)      # BEFORE the tool body runs — fsync'd so it is durable
    mark_completed(id)    # AFTER a successful body (carries the result forward)
      -- or --
    mark_failed(id)       # AFTER a failed body

A crash between ``mark_started`` and a terminal leaves the record in ``started``
state: :meth:`unresolved` surfaces exactly those "unknown after crash" calls.
For an EXTERNAL call the framework never guesses whether its effect took hold —
that judgment (verify / retry / abandon) belongs to the model, not the ledger.

Storage is the shared run journal (:class:`~mote.runtime.ledger.RunJournal`): an
EXTERNAL tool record is simply a :class:`~mote.runtime.ledger.StepRecord` with
``kind="tool"`` / ``effect="external"`` and ``step_id == tool_call_id``. This
class is a thin tool-facing *view* over that journal — it keeps the historical
``mark_started`` / ``mark_completed`` / ``mark_failed`` / ``status`` /
``unresolved`` / ``reap`` API and the :class:`EffectRecord` shape the resume
reconciler reads, mapping each ``kind="tool"`` step to an ``EffectRecord`` on the
way out. Because the journal is the single durable substrate, a think / timer /
LOCAL-tool step recorded by the loop's durable runner shares the very same file,
so a resume sees every step under one log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from mote.runtime.ledger import COMPLETED, FAILED, JOURNAL_FILE_NAME, KIND_TOOL, STARTED, RunJournal, StepRecord
from mote.runtime.workspace import WorkspaceStore

#: Filename of the append-only ledger inside a session's ``ledger/`` space.
#: Aliased onto the shared journal file so the EXTERNAL-tool view and the run
#: journal are the same physical log (zero-migration absorption).
LEDGER_FILE_NAME = JOURNAL_FILE_NAME


@dataclass(frozen=True)
class EffectRecord:
    """One folded ledger entry for a single EXTERNAL tool call.

    The tool-facing projection of a ``kind="tool"`` :class:`StepRecord`. ``result``
    holds a versioned full ``ToolResult`` receipt for current terminal records,
    or the output string for legacy records. The resume path understands both,
    so it can heal a dangling call without re-running the tool. ``started``
    records leave it ``None``.
    """

    tool_call_id: str
    tool_name: str
    status: str
    started_at: float
    ended_at: Optional[float] = None
    result: Optional[str] = None
    success: bool = True
    #: The call's side-effect class (see :class:`ToolEffect`). Recorded so the
    #: resume reconciler can tell a replay-safe PURE/LOCAL call from an EXTERNAL
    #: one whose in-flight outcome is unknowable. A legacy line predating this
    #: field folds in as EXTERNAL (fail-closed — the only calls this ledger has
    #: ever recorded), so an untagged dangling call is never blindly replayed.
    effect: str = "external"

    def to_json(self) -> str:
        return json.dumps(
            {
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "result": self.result,
                "success": self.success,
                "effect": self.effect,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "EffectRecord":
        return cls(
            tool_call_id=d["tool_call_id"],
            tool_name=d.get("tool_name", ""),
            status=d.get("status", STARTED),
            started_at=d.get("started_at", 0.0),
            ended_at=d.get("ended_at"),
            result=d.get("result"),
            success=d.get("success", True),
            effect=d.get("effect", "external"),
        )

    @classmethod
    def from_step(cls, rec: StepRecord) -> "EffectRecord":
        """Project a ``kind="tool"`` journal step onto the tool-facing shape."""
        return cls(
            tool_call_id=rec.tool_call_id or rec.step_id,
            tool_name=rec.name,
            status=rec.status,
            started_at=rec.started_at,
            ended_at=rec.ended_at,
            result=rec.payload,
            success=rec.success,
            effect=rec.effect,
        )


class EffectLedger:
    """Durable started/completed/failed view for one session's EXTERNAL calls.

    A thin tool-facing writer/reader over a shared :class:`RunJournal`. Cheap to
    construct: the journal folds any existing on-disk log into an in-memory
    latest-per-id index (so status/unresolved are O(1)). A fresh instance for the
    same ``session_id`` — e.g. one built by the resume reconciler in a new
    process — therefore sees the pre-crash state.

    Construct with ``(session_id, store)`` to own a private journal, or with an
    injected ``journal=`` so the executor can share ONE journal between this
    EXTERNAL-effect view and the loop's durable runner (think/timer/LOCAL steps).
    """

    def __init__(
        self,
        session_id: str | None = None,
        store: WorkspaceStore | None = None,
        *,
        journal: RunJournal | None = None,
    ) -> None:
        if journal is not None:
            self._journal = journal
        else:
            if session_id is None:
                raise ValueError("EffectLedger needs either a session_id or an injected journal")
            self._journal = RunJournal(session_id, store=store)
        # Kept for the historical ``ledger._store`` accessor some callers use.
        self._store = self._journal.store

    @property
    def journal(self) -> RunJournal:
        """The shared run journal this view reads/writes (the durable substrate)."""
        return self._journal

    @property
    def path(self):
        """The resolved JSONL file backing the journal."""
        return self._journal.path

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def status(self, tool_call_id: str) -> Optional[EffectRecord]:
        """The latest record for *tool_call_id*, or ``None`` if never seen."""
        rec = self._journal.replay(tool_call_id)
        return EffectRecord.from_step(rec) if rec is not None else None

    def unresolved(self) -> list[EffectRecord]:
        """Every EXTERNAL-tool call whose latest state is ``started`` — i.e.
        unknown after a crash. Non-tool steps (think/timer) are not surfaced here."""
        return [EffectRecord.from_step(r) for r in self._journal.unresolved() if r.kind == KIND_TOOL]

    # ------------------------------------------------------------------
    # Writes (each appends one durable line and updates the in-memory index)
    # ------------------------------------------------------------------

    def mark_started(self, tool_call_id: str, tool_name: str, *, effect: str = "external") -> None:
        """Record that a call is about to run. MUST be durable before the tool
        body executes, so a crash mid-call is detectable on resume.

        ``effect`` is the call's side-effect class (see :class:`ToolEffect`);
        it defaults to ``"external"`` — the only class this ledger historically
        recorded — and is carried on the record so the resume reconciler can tell
        a replay-safe PURE/LOCAL call from an EXTERNAL one whose outcome is
        unknowable after a crash.
        """
        self._journal.record_started(tool_call_id, KIND_TOOL, effect, name=tool_name, tool_call_id=tool_call_id)

    def mark_completed(self, tool_call_id: str, tool_name: str, *, result: Optional[str] = None) -> None:
        """Record a successful terminal, carrying the result forward for healing."""
        self._journal.record_completed(tool_call_id, payload=result, success=True)

    def mark_failed(self, tool_call_id: str, tool_name: str, *, result: Optional[str] = None) -> None:
        """Record a failed terminal (the call ran but errored)."""
        self._journal.record_failed(tool_call_id, payload=result)

    def reap(self, ids: Iterable[str]) -> None:
        """Drop resolved call ids and rewrite the folded log (bounded growth)."""
        self._journal.reap(ids)


__all__ = [
    "EffectLedger",
    "EffectRecord",
    "STARTED",
    "COMPLETED",
    "FAILED",
    "LEDGER_FILE_NAME",
]
