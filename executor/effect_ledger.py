"""EffectLedger — a per-session, durable record of EXTERNAL tool-call effects.

The gap it closes: a tool whose effect escapes the locally-recoverable boundary
(network / IPC / subprocess / a human-visible action / a spawned agent) has no
before-image snapshot, so if the process crashes *after* the effect happened but
*before* the tool-result message is durably flushed, a naive resume would replay
the call and duplicate the effect. This ledger is the missing bookkeeping: a
tiny append-only record per ``(session_id, tool_call_id)`` that survives a crash
and lets the resume reconciler tell an in-flight call from a finished one.

Only ``ToolEffect.EXTERNAL`` calls are ledgered (PURE reads and LOCAL fs writes
are already replay-safe — see :meth:`BaseTool.resolve_effect`).

Lifecycle of one call, written at the ``ToolExecutor.run_command`` chokepoint:

    mark_started(id)      # BEFORE the tool body runs — fsync'd so it is durable
    mark_completed(id)    # AFTER a successful body (carries the result forward)
      -- or --
    mark_failed(id)       # AFTER a failed body

A crash between ``mark_started`` and a terminal leaves the record in ``started``
state: :meth:`unresolved` surfaces exactly those "unknown after crash" calls.
The framework never guesses whether such a call's EXTERNAL effect took hold —
that judgment (verify / retry / abandon) belongs to the model, not the ledger.

Storage mirrors the rollout log: an append-only JSONL under the session's
``ledger/`` space, folded on read to the latest record per call id. Disk I/O
goes through :mod:`mote.common.disk.disk_io` (the shared append+fsync primitive),
and the location is resolved through :class:`WorkspaceStore` so the ledger
co-locates under the session directory and is swept with it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from mote.common.disk import disk_io
from mote.common.logs import logger
from mote.common.workspace import ArtifactKind, WorkspaceStore

#: Filename of the append-only ledger inside a session's ``ledger/`` space.
LEDGER_FILE_NAME = "effects.jsonl"

#: The three lifecycle states a ledger record can carry.
STARTED = "started"
COMPLETED = "completed"
FAILED = "failed"


@dataclass(frozen=True)
class EffectRecord:
    """One folded ledger entry for a single EXTERNAL tool call.

    ``result`` holds the tool's final (post-size-limit) output string for a
    terminal record, so the resume reconciler can heal a dangling call whose
    result message was never flushed — without re-running the tool. ``started``
    records leave it ``None``.
    """

    tool_call_id: str
    tool_name: str
    status: str
    started_at: float
    ended_at: Optional[float] = None
    result: Optional[str] = None
    success: bool = True

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
        )


class EffectLedger:
    """Durable started/completed/failed ledger for one session's EXTERNAL calls.

    Cheap to construct: it folds any existing on-disk ledger into an in-memory
    ``latest``-per-id index (so status/unresolved are O(1)). A fresh instance for
    the same ``session_id`` — e.g. one built by the resume reconciler in a new
    process — therefore sees the pre-crash state.
    """

    def __init__(self, session_id: str, store: WorkspaceStore | None = None) -> None:
        self._session_id = session_id
        self._store = store or WorkspaceStore()
        self._latest: dict[str, EffectRecord] = {}
        self._load()

    @property
    def path(self) -> Path:
        """The session's ledger file (``ledger/effects.jsonl``)."""
        return self._store.space(self._session_id, ArtifactKind.LEDGER) / LEDGER_FILE_NAME

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def status(self, tool_call_id: str) -> Optional[EffectRecord]:
        """The latest record for *tool_call_id*, or ``None`` if never seen."""
        return self._latest.get(tool_call_id)

    def unresolved(self) -> list[EffectRecord]:
        """Every call whose latest state is ``started`` — i.e. unknown after a crash."""
        return [r for r in self._latest.values() if r.status == STARTED]

    # ------------------------------------------------------------------
    # Writes (each appends one durable line and updates the in-memory index)
    # ------------------------------------------------------------------

    def mark_started(self, tool_call_id: str, tool_name: str) -> None:
        """Record that an EXTERNAL call is about to run. MUST be durable before
        the tool body executes, so a crash mid-call is detectable on resume."""
        self._append(
            EffectRecord(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=STARTED,
                started_at=time.time(),
            )
        )

    def mark_completed(self, tool_call_id: str, tool_name: str, *, result: Optional[str] = None) -> None:
        """Record a successful terminal, carrying the result forward for healing."""
        self._terminal(tool_call_id, tool_name, COMPLETED, result=result, success=True)

    def mark_failed(self, tool_call_id: str, tool_name: str, *, result: Optional[str] = None) -> None:
        """Record a failed terminal (the call ran but errored)."""
        self._terminal(tool_call_id, tool_name, FAILED, result=result, success=False)

    def reap(self, tool_call_ids: Iterable[str]) -> None:
        """Drop the given calls from the ledger (after the reconciler resolves them).

        Rewrites the file to the remaining folded records atomically, so the
        ledger does not grow without bound across a long-lived session.
        """
        ids = set(tool_call_ids)
        if not ids:
            return
        for cid in ids:
            self._latest.pop(cid, None)
        self._rewrite()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _terminal(
        self, tool_call_id: str, tool_name: str, status: str, *, result: Optional[str], success: bool
    ) -> None:
        prior = self._latest.get(tool_call_id)
        self._append(
            EffectRecord(
                tool_call_id=tool_call_id,
                tool_name=tool_name or (prior.tool_name if prior else ""),
                status=status,
                started_at=prior.started_at if prior is not None else time.time(),
                ended_at=time.time(),
                result=result,
                success=success,
            )
        )

    def _append(self, record: EffectRecord) -> None:
        self._latest[record.tool_call_id] = record
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            disk_io.append_line(self.path, record.to_json(), fsync=True)
        except OSError as e:
            # Best-effort durability: a write failure must never break tool
            # execution. The in-memory index still reflects the record for this
            # process; only cross-crash durability is lost.
            logger.warning(f"Failed to append effect-ledger record {self.path}: {e}")

    def _rewrite(self) -> None:
        try:
            lines = "".join(f"{r.to_json()}\n" for r in self._latest.values())
            disk_io.atomic_write(self.path, lines.encode("utf-8"), fsync=True)
        except OSError as e:
            logger.warning(f"Failed to rewrite effect ledger {self.path}: {e}")

    def _load(self) -> None:
        path = self.path
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"Failed to read effect ledger {path}: {e}")
            return
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = EffectRecord.from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # skip a torn/garbled line, keep folding the rest
            self._latest[record.tool_call_id] = record


__all__ = [
    "EffectLedger",
    "EffectRecord",
    "STARTED",
    "COMPLETED",
    "FAILED",
    "LEDGER_FILE_NAME",
]
