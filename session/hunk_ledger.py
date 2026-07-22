"""HunkLedger — a per-session, durable record of tracked change *hunks*.

Where :class:`~mote.executor.effect_ledger.EffectLedger` records the lifecycle
of EXTERNAL tool *effects* (idempotency / crash reconciliation), this ledger
records the *change attribution* of every file hunk the session touches: which
turn / tool produced it, whether it came from the agent or an external (human)
edit, its line geometry, and its accept/reject status. It is the durable truth
source a review UI reads to render "pending changes grouped by turn" and to
drive per-hunk accept / reject / undo.

A :class:`HunkRecord` is deliberately *thin* — it carries geometry
(``old_range`` / ``new_range``), attribution (``source`` / ``turn_index`` /
``tool_call_id``) and a ``pre_hash`` that references the file's before-image
blob (the very blob :class:`~mote.session.snapshot.FileSnapshotRecorder` already
stores before a write). The hunk's *text* is never duplicated here: the old side
is reconstructed by slicing the pre-image blob at ``old_range``, the new side by
slicing the *live* file at ``new_range`` (a hunk is a pending change of baseline
→ current, exactly as the snapshot layer models it). This keeps the log small
and keeps the blob store the single home for content.

Storage mirrors the effect ledger: an append-only JSONL under the session's
``ledger/`` space, folded on read to the latest record per ``hunk_id`` via the
shared :class:`~mote.common.ledger.AppendOnlyLedger` base. This subclass adds
only the record shape and its fold key; a status change (accept / reject) is a
fresh append that folds over the prior record for the same id.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Optional

from mote.common.ledger import AppendOnlyLedger
from mote.common.text.hunks import split_hunks
from mote.common.workspace import ArtifactKind, WorkspaceStore

#: Filename of the append-only hunk ledger inside a session's ``ledger/`` space.
LEDGER_FILE_NAME = "hunks.jsonl"

#: Where a hunk came from.
AGENT = "agent"  # written directly by an agent tool at ``turn_index``
EXTERNAL = "external"  # external (human) edit detected by the read-before-write guard

#: The accept/reject lifecycle a hunk record can carry.
PENDING = "pending"  # detected, not yet acted on
ACCEPTED = "accepted"  # folded into the baseline (change kept)
REJECTED = "rejected"  # reverted on disk (change discarded)


@dataclass(frozen=True)
class HunkRecord:
    """One folded ledger entry for a single tracked change hunk.

    ``old_range``/``new_range`` are ``(start, count)`` line spans (1-indexed,
    unified-diff convention; a pure insertion has ``old_count == 0``, a pure
    deletion ``new_count == 0``). ``pre_hash`` is the sha256 digest of the file's
    full pre-change content — the same key the before-image blob is stored under
    — so the old side can be reconstructed without duplicating text here.
    """

    hunk_id: str
    path: str
    session_id: str
    tool_call_id: str
    turn_index: int
    source: str
    old_range: tuple[int, int]
    new_range: tuple[int, int]
    pre_hash: str
    status: str = PENDING
    ts: float = field(default_factory=time.time)

    @property
    def is_agent(self) -> bool:
        """True when the agent wrote this hunk directly."""
        return self.source == AGENT

    @property
    def is_external(self) -> bool:
        """True for an external (human) edit."""
        return self.source == EXTERNAL

    def to_json(self) -> str:
        return json.dumps(
            {
                "hunk_id": self.hunk_id,
                "path": self.path,
                "session_id": self.session_id,
                "tool_call_id": self.tool_call_id,
                "turn_index": self.turn_index,
                "source": self.source,
                "old_range": list(self.old_range),
                "new_range": list(self.new_range),
                "pre_hash": self.pre_hash,
                "status": self.status,
                "ts": self.ts,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "HunkRecord":
        old = d.get("old_range") or [0, 0]
        new = d.get("new_range") or [0, 0]
        return cls(
            hunk_id=d["hunk_id"],
            path=d.get("path", ""),
            session_id=d.get("session_id", ""),
            tool_call_id=d.get("tool_call_id", ""),
            turn_index=d.get("turn_index", 0),
            source=d.get("source", AGENT),
            old_range=(int(old[0]), int(old[1])),
            new_range=(int(new[0]), int(new[1])),
            pre_hash=d.get("pre_hash", ""),
            status=d.get("status", PENDING),
            ts=d.get("ts", 0.0),
        )

    def with_status(self, status: str) -> "HunkRecord":
        """A copy of this record carrying a new lifecycle *status* (fresh ``ts``)."""
        return replace(self, status=status, ts=time.time())


class HunkLedger(AppendOnlyLedger[HunkRecord]):
    """Durable, fold-by-``hunk_id`` ledger for one session's change hunks.

    Cheap to construct: the base folds any existing on-disk ledger into an
    in-memory ``latest``-per-id index. A fresh instance for the same
    ``session_id`` — e.g. one rebuilt on resume in a new process — therefore
    sees the pre-crash hunks and their latest status.
    """

    def __init__(self, session_id: str, store: WorkspaceStore | None = None) -> None:
        self._session_id = session_id
        self._store = store or WorkspaceStore()
        path = self._store.space(session_id, ArtifactKind.LEDGER) / LEDGER_FILE_NAME
        super().__init__(path)

    # ------------------------------------------------------------------
    # Base hooks
    # ------------------------------------------------------------------

    def _parse_record(self, data: dict) -> HunkRecord:
        return HunkRecord.from_dict(data)

    def _record_key(self, record: HunkRecord) -> str:
        return record.hunk_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def status(self, hunk_id: str) -> Optional[HunkRecord]:
        """The latest record for *hunk_id*, or ``None`` if never seen."""
        return self.get(hunk_id)

    def pending(self) -> list[HunkRecord]:
        """Every hunk whose latest status is ``pending`` (awaiting accept/reject)."""
        return [r for r in self.records() if r.status == PENDING]

    def for_turn(self, turn_index: int) -> list[HunkRecord]:
        """Every hunk attributed to *turn_index* (any status)."""
        return [r for r in self.records() if r.turn_index == turn_index]

    def for_path(self, path: str) -> list[HunkRecord]:
        """Every hunk touching *path* (any status)."""
        return [r for r in self.records() if r.path == path]

    # ------------------------------------------------------------------
    # Writes (each appends one durable line and folds the in-memory index)
    # ------------------------------------------------------------------

    def record(self, record: HunkRecord) -> None:
        """Durably append a freshly-detected hunk (folds over any prior id)."""
        self.append(record)

    def record_delta(
        self,
        blobs,
        *,
        path: str,
        old: str,
        new: str,
        source: str,
        turn_index: int,
        tool_call_id: str = "",
        id_base: str,
    ) -> list[HunkRecord]:
        """Split an ``old`` → ``new`` delta into hunks and record each durably.

        The single capture primitive shared by both hunk producers (the agent
        :class:`~mote.session.subscribers.HunkSubscriber` and the read-guard's
        ``attribute_external_change``): they differ only in ``source`` /
        ``id_base`` / ``tool_call_id``, so the delta → split → per-hunk-record
        loop lives here once.

        ``old`` is stored via ``blobs.put`` and the **returned** digest is used
        as every record's ``pre_hash``. Using the store's own return value keeps
        the key backend-native — sha256 for :class:`~mote.session.snapshot.BlobStore`,
        the git object id for :class:`~mote.session.snapshot.GitBlobStore` — so
        the rehydrate path (``blobs.get(pre_hash)``) fetches the before-image
        back under *any* backend. ``put`` is idempotent, so re-storing the same
        before-image the snapshot recorder already holds is a cheap no-op.

        Each hunk is recorded under ``f"{id_base}:{i}"``; a stable ``id_base``
        makes a resume replay fold onto the same records rather than duplicate
        them. Returns the recorded rows (empty when the texts are identical).
        """
        hunks = split_hunks(old, new)
        if not hunks:
            return []
        pre_hash = blobs.put(old.encode("utf-8"))
        recorded: list[HunkRecord] = []
        for i, hunk in enumerate(hunks):
            rec = HunkRecord(
                hunk_id=f"{id_base}:{i}",
                path=path,
                session_id=self._session_id,
                tool_call_id=tool_call_id,
                turn_index=turn_index,
                source=source,
                old_range=(hunk.old_start, hunk.old_count),
                new_range=(hunk.new_start, hunk.new_count),
                pre_hash=pre_hash,
            )
            self.append(rec)
            recorded.append(rec)
        return recorded

    def set_status(self, hunk_id: str, status: str) -> Optional[HunkRecord]:
        """Fold a status change onto an existing hunk; ``None`` if unknown.

        Returns the new folded record so the caller can act on its geometry
        (e.g. revert on disk) without a second lookup.
        """
        prior = self.get(hunk_id)
        if prior is None:
            return None
        updated = prior.with_status(status)
        self.append(updated)
        return updated


__all__ = [
    "HunkLedger",
    "HunkRecord",
    "AGENT",
    "EXTERNAL",
    "PENDING",
    "ACCEPTED",
    "REJECTED",
    "LEDGER_FILE_NAME",
]
