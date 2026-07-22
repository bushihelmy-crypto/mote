"""Hunk attribution queries — the read-side contract a review UI consumes.

Where :mod:`~mote.session.hunk_ledger` is the *durable record* (thin geometry +
attribution, no duplicated text) and :mod:`~mote.session.hunk_ops` is the
*write-side* engine (accept / reject / undo), this module is the *read-side*
projection: it groups the ledger's records the way a reviewer wants to see them
(by turn, by file, by source, or the whole-session tally) and *rehydrates* each
hunk's before/after text on demand.

**Body rehydration mirrors the ledger's own contract.** A record stores only
line geometry + a ``pre_hash``; the text lives in the blob store. So the OLD
side is reconstructed by slicing the before-image blob (keyed by ``pre_hash``)
at ``old_range``, and the NEW side by slicing the *live* file at ``new_range``
(a hunk is a pending change of baseline → current, exactly as the snapshot layer
models it). Rehydration is best-effort: a missing blob or an unreadable /
drifted file yields empty text for that side rather than raising, so a review UI
can always render *something* (the geometry + attribution are always present).

**Layering.** This is a pure session-layer reader over a :class:`HunkLedger`
plus a content-addressed blob store (``.get(digest)``); it imports neither
``roles`` nor the write-side engine, so it can back a CLI review command or a
programmatic caller without pulling in either.
"""

from __future__ import annotations

from dataclasses import dataclass

from mote.common.text.hunks import slice_lines
from mote.session.hunk_ledger import ACCEPTED, PENDING, REJECTED, HunkLedger, HunkRecord
from mote.session.hunk_rehydrate import blob_text, read_current

__all__ = ["HunkView", "SessionSummary", "HunkAttribution"]


@dataclass(frozen=True)
class HunkView:
    """A ledger record plus its rehydrated before/after text — one review row.

    ``old_text`` is the removed/changed baseline lines (sliced from the
    before-image blob at ``old_range``); ``new_text`` is the added/changed
    current lines (sliced from the live file at ``new_range``). Either may be
    ``""`` — for a pure insertion/deletion by geometry, or when the source
    content is unavailable (missing blob / unreadable file).
    """

    record: HunkRecord
    old_text: str
    new_text: str

    # --- Convenience passthroughs (so a UI need not reach into ``record``) ---
    @property
    def hunk_id(self) -> str:
        return self.record.hunk_id

    @property
    def path(self) -> str:
        return self.record.path

    @property
    def source(self) -> str:
        return self.record.source

    @property
    def status(self) -> str:
        return self.record.status

    @property
    def turn_index(self) -> int:
        return self.record.turn_index

    @property
    def is_pending(self) -> bool:
        return self.record.status == PENDING


@dataclass(frozen=True)
class SessionSummary:
    """A whole-session tally for a review header / status line.

    ``by_source`` / ``by_status`` / ``by_path`` count every record (any status)
    keyed by that dimension; ``files`` is the sorted set of touched paths.
    """

    total: int
    pending: int
    accepted: int
    rejected: int
    by_source: dict[str, int]
    by_status: dict[str, int]
    by_path: dict[str, int]
    files: list[str]


class HunkAttribution:
    """Read-side projection of one session's :class:`HunkLedger` for review.

    Constructed with the ledger plus the content-addressed blob store
    (``.get(digest)``) that holds the before-image blobs a record's ``pre_hash``
    keys. Every query returns :class:`HunkView` rows with text rehydrated on
    demand; the whole-session tally is :meth:`session_summary`.
    """

    def __init__(self, ledger: HunkLedger, blobs) -> None:
        self._ledger = ledger
        self._blobs = blobs

    # ------------------------------------------------------------------
    # Grouped queries (each returns rehydrated views)
    # ------------------------------------------------------------------

    def all_hunks(self) -> list[HunkView]:
        """Every recorded hunk (any status), as review rows."""
        return [self._view(r) for r in self._ledger.records()]

    def hunks_for_turn(self, turn_index: int) -> list[HunkView]:
        """Every hunk attributed to *turn_index* (any status)."""
        return [self._view(r) for r in self._ledger.for_turn(turn_index)]

    def hunks_for_file(self, path: str) -> list[HunkView]:
        """Every hunk touching *path* (any status), top-of-file first."""
        rows = [self._view(r) for r in self._ledger.for_path(path)]
        return sorted(rows, key=lambda v: v.record.new_range[0])

    def hunks_by_source(self, source: str) -> list[HunkView]:
        """Every hunk whose ``source`` equals *source* (e.g. ``agent`` / ``external``)."""
        return [self._view(r) for r in self._ledger.records() if r.source == source]

    def pending(self) -> list[HunkView]:
        """Every hunk still awaiting a review decision."""
        return [self._view(r) for r in self._ledger.pending()]

    # ------------------------------------------------------------------
    # Whole-session tally
    # ------------------------------------------------------------------

    def session_summary(self) -> SessionSummary:
        """Aggregate counts across the whole ledger (any status)."""
        records = self._ledger.records()
        by_source: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_path: dict[str, int] = {}
        for r in records:
            by_source[r.source] = by_source.get(r.source, 0) + 1
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_path[r.path] = by_path.get(r.path, 0) + 1
        return SessionSummary(
            total=len(records),
            pending=by_status.get(PENDING, 0),
            accepted=by_status.get(ACCEPTED, 0),
            rejected=by_status.get(REJECTED, 0),
            by_source=by_source,
            by_status=by_status,
            by_path=by_path,
            files=sorted(by_path),
        )

    # ------------------------------------------------------------------
    # Internals — body rehydration
    # ------------------------------------------------------------------

    def _view(self, rec: HunkRecord) -> HunkView:
        old = slice_lines(blob_text(self._blobs, rec.pre_hash), *rec.old_range)
        new = slice_lines(read_current(rec.path), *rec.new_range)
        return HunkView(record=rec, old_text=old, new_text=new)
