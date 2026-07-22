"""HunkOps — accept / reject / undo operations over tracked change hunks.

Where :mod:`~mote.session.hunk_ledger` is the *durable record* of every tracked
hunk (geometry + attribution + status) and :mod:`~mote.common.text.hunks` is the
*pure algebra* (apply / revert a hunk against text), this module is the
*coordination layer* that turns a review decision — "accept this hunk", "reject
that turn's changes", "undo everything" — into the right combination of ledger
status change, on-disk revert, and known-baseline advance.

**Two anchors, kept distinct.** A pending :class:`~mote.session.hunk_ledger.HunkRecord`
carries its own ``pre_hash`` — the digest of the file's *full pre-change content*
(the very before-image blob the snapshot recorder already stores) — which is the
**old** side of that hunk. The **new** side is the *live file*. So the applicable
:class:`~mote.common.text.hunks.Hunk` for a record is re-derived fresh from
``(pre_content, current)`` via :func:`~mote.common.text.hunks.split_hunks` and
matched by geometry; if the file has drifted so no live change matches the record
(e.g. a later edit reshaped the region), the operation fails cleanly as
``drifted`` rather than corrupting the file. The separate per-path *known
baseline* (the content mote treats as authoritative for external-change
attribution) is advanced by every operation to track whatever ends up on disk, so
attribution never mis-flags an accepted/reverted region as an external edit.

Two operations, mirroring the two sides of a pending change:

* **accept** — the change is kept, so the file on disk is already correct: no
  disk write. The hunk is marked ``accepted`` (it drops out of ``pending``) and
  the known baseline is advanced to the current on-disk content.
* **reject / undo** — the change is discarded, so the file must be reverted on
  disk. The hunk's ``new`` region is replaced by its ``old`` content via
  :func:`~mote.common.text.hunks.revert_hunk`, written atomically, and the
  session's read-state is refreshed so the read-before-write guard still passes.
  The known baseline is advanced to the reverted content, and the remaining
  pending hunks below it have their ``new_range`` shifted by the line delta so
  their geometry stays valid against the changed file.

**Layering.** This lives in the session layer and stays independent of the roles
layer: the two pieces of per-Role state it must touch (the known-baseline digest
map and the file-read map) are reached through injected callbacks, not by
importing ``roles`` (which would invert the ``roles → session`` dependency). The
Role wires those callbacks onto its own state controller.

Batches process highest line first per file so earlier operations never
invalidate the line numbers of the ones still to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from mote.common.disk import atomic_write
from mote.common.logs import logger
from mote.common.text.hunks import Hunk, HunkApplyError, revert_hunk, split_hunks
from mote.session.hunk_ledger import ACCEPTED, PENDING, REJECTED, HunkLedger, HunkRecord
from mote.session.hunk_rehydrate import blob_text, read_current

__all__ = ["HunkOps", "HunkOpResult", "BatchOpResult"]


@dataclass(frozen=True)
class HunkOpResult:
    """The outcome of a single accept/reject operation on one hunk."""

    hunk_id: str
    ok: bool
    status: str = ""
    """The new lifecycle status when ``ok`` (``accepted`` / ``rejected``)."""
    error: str = ""
    """A short reason when not ``ok`` (``unknown`` / ``not pending`` / ``drifted`` / …)."""


@dataclass(frozen=True)
class BatchOpResult:
    """The aggregate outcome of a batch operation (by file / turn / all)."""

    results: list[HunkOpResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every hunk in the batch succeeded (vacuously true if empty)."""
        return all(r.ok for r in self.results)

    @property
    def succeeded(self) -> list[HunkOpResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[HunkOpResult]:
        return [r for r in self.results if not r.ok]


class HunkOps:
    """Accept / reject / undo engine over one session's :class:`HunkLedger`.

    Constructed with the ledger plus the narrow, roles-independent hooks it needs
    to advance the known baseline and refresh read-state:

    * ``blobs`` — the content-addressed store (``.get(digest)`` / ``.put(bytes)``)
      that holds before-image blobs (``FileSnapshotRecorder.blobs``); a record's
      ``pre_hash`` keys its pre-change content there.
    * ``set_baseline(path, digest)`` — record the per-path known-baseline digest
      (so external-change attribution tracks the post-operation on-disk content).
    * ``refresh_read_state(path)`` — re-record the file's read mtime after a
      revert writes it, so the read-before-write guard still passes.
    """

    def __init__(
        self,
        ledger: HunkLedger,
        blobs,
        *,
        set_baseline: Callable[[str, str], None],
        refresh_read_state: Callable[[str], None],
    ) -> None:
        self._ledger = ledger
        self._blobs = blobs
        self._set_baseline = set_baseline
        self._refresh_read_state = refresh_read_state

    # ------------------------------------------------------------------
    # Single-hunk operations
    # ------------------------------------------------------------------

    def accept(self, hunk_id: str) -> HunkOpResult:
        """Keep the change: mark the hunk accepted and advance the baseline.

        The file on disk is already what the agent wrote, so accepting does no
        disk write — it marks the hunk ``accepted`` (dropping it from ``pending``)
        and advances the known baseline to the current on-disk content.
        """
        rec = self._ledger.status(hunk_id)
        if rec is None:
            return HunkOpResult(hunk_id, False, error="unknown")
        if rec.status != PENDING:
            return HunkOpResult(hunk_id, False, error="not pending")

        self._advance_baseline(rec.path, read_current(rec.path))
        self._ledger.set_status(hunk_id, ACCEPTED)
        return HunkOpResult(hunk_id, True, status=ACCEPTED)

    def reject(self, hunk_id: str) -> HunkOpResult:
        """Discard the change: revert the hunk on disk and mark it rejected.

        Replaces the hunk's ``new`` region with its ``old`` content, writes the
        file atomically, refreshes read-state, advances the known baseline to the
        reverted content, and shifts the remaining pending hunks below it.
        """
        rec = self._ledger.status(hunk_id)
        if rec is None:
            return HunkOpResult(hunk_id, False, error="unknown")
        if rec.status != PENDING:
            return HunkOpResult(hunk_id, False, error="not pending")

        current, hunk = self._resolve(rec)
        if hunk is None:
            return HunkOpResult(hunk_id, False, error="drifted")
        try:
            new_current = revert_hunk(current, hunk)
        except HunkApplyError as exc:
            return HunkOpResult(hunk_id, False, error=f"drifted: {exc}")

        try:
            atomic_write(Path(rec.path), new_current.encode("utf-8"))
        except OSError as exc:
            logger.warning(f"HunkOps.reject: cannot write '{rec.path}': {exc}")
            return HunkOpResult(hunk_id, False, error=f"write failed: {exc}")
        self._refresh_read_state(rec.path)
        self._advance_baseline(rec.path, new_current)
        # File shrank/grew by (old_count - new_count) lines at this hunk; shift the
        # ``new_range`` of every OTHER pending hunk below it (their old anchors,
        # keyed by their own ``pre_hash``, are unaffected by a revert on disk).
        self._shift_after_reject(
            rec.path,
            exclude_id=hunk_id,
            threshold=hunk.new_start,
            delta=hunk.old_count - hunk.new_count,
        )
        self._ledger.set_status(hunk_id, REJECTED)
        return HunkOpResult(hunk_id, True, status=REJECTED)

    #: ``undo`` is a spelling of ``reject`` — both revert the change on disk.
    undo = reject

    # ------------------------------------------------------------------
    # Batch operations (by file / turn / all)
    # ------------------------------------------------------------------

    def accept_file(self, path: str) -> BatchOpResult:
        """Accept every pending hunk touching ``path``."""
        return BatchOpResult([self.accept(r.hunk_id) for r in self._pending_for(path=path)])

    def reject_file(self, path: str) -> BatchOpResult:
        """Reject every pending hunk touching ``path`` (highest line first)."""
        return self._reject_batch(self._pending_for(path=path))

    def accept_turn(self, turn_index: int) -> BatchOpResult:
        """Accept every pending hunk attributed to ``turn_index``."""
        return BatchOpResult([self.accept(r.hunk_id) for r in self._pending_for(turn_index=turn_index)])

    def reject_turn(self, turn_index: int) -> BatchOpResult:
        """Reject every pending hunk attributed to ``turn_index`` (highest line first)."""
        return self._reject_batch(self._pending_for(turn_index=turn_index))

    def accept_all(self) -> BatchOpResult:
        """Accept every pending hunk in the session."""
        return BatchOpResult([self.accept(r.hunk_id) for r in self._ledger.pending()])

    def reject_all(self) -> BatchOpResult:
        """Reject (undo) every pending hunk in the session (highest line first)."""
        return self._reject_batch(self._ledger.pending())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pending_for(self, *, path: Optional[str] = None, turn_index: Optional[int] = None) -> list[HunkRecord]:
        return [
            r
            for r in self._ledger.pending()
            if (path is None or r.path == path) and (turn_index is None or r.turn_index == turn_index)
        ]

    def _reject_batch(self, records: list[HunkRecord]) -> BatchOpResult:
        # Highest ``new_start`` first per file: reverting a lower hunk never moves
        # a higher one's current lines, so each still-pending target stays valid.
        ordered = sorted(records, key=lambda r: (r.path, r.new_range[0]), reverse=True)
        return BatchOpResult([self.reject(r.hunk_id) for r in ordered])

    def _resolve(self, rec: HunkRecord) -> tuple[str, Optional[Hunk]]:
        """Return ``(current, hunk)`` for ``rec`` from live content.

        The old side is the record's before-image blob (keyed by ``pre_hash``);
        the new side is the live file. ``hunk`` is the live change whose geometry
        matches the record, or ``None`` when the file has drifted so the record no
        longer corresponds to any current change (the caller reports ``drifted``).
        """
        old = blob_text(self._blobs, rec.pre_hash)
        current = read_current(rec.path)
        for hunk in split_hunks(old, current):
            if (
                hunk.old_start == rec.old_range[0]
                and hunk.old_count == rec.old_range[1]
                and hunk.new_start == rec.new_range[0]
                and hunk.new_count == rec.new_range[1]
            ):
                return current, hunk
        return current, None

    def _advance_baseline(self, path: str, content: str) -> None:
        """Record ``content`` as the file's known baseline (best-effort blob put)."""
        try:
            digest = self._blobs.put(content.encode("utf-8"))
            self._set_baseline(path, digest)
        except Exception as exc:  # noqa: BLE001 — baseline bookkeeping must not fail an op
            logger.warning(f"HunkOps: could not advance baseline for '{path}': {exc}")

    def _shift_after_reject(self, path: str, *, exclude_id: str, threshold: int, delta: int) -> None:
        """Shift the ``new_range`` of remaining pending hunks below the revert.

        A revert changes only the current-side line numbers, so records on
        ``path`` whose ``new_start`` is below the reverted hunk shift by ``delta``
        (the current line-count change); their ``pre_hash`` old anchors are
        untouched.
        """
        if delta == 0:
            return
        for r in self._ledger.for_path(path):
            if r.hunk_id == exclude_id or r.status != PENDING:
                continue
            if r.new_range[0] > threshold:
                self._ledger.record(replace(r, new_range=(r.new_range[0] + delta, r.new_range[1])))
