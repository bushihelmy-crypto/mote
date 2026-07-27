"""Runtime crash-durable, fold-by-key JSONL ledger primitive.

Several subsystems need the same bookkeeping shape: an append-only log of small
records, each keyed by a stable id, written durably (fsync) *before* the thing
it records happens, and folded on read to the latest record per key so a fresh
instance — e.g. one rebuilt by a resume path in a new process — sees the
pre-crash state. The :class:`~mote.runtime.tools.effect_ledger.EffectLedger`
(EXTERNAL tool-effect idempotency) and the session hunk ledger (change
attribution) are both exactly this.

This base owns the mechanics — append / fold / rewrite (bounded growth) /
best-effort durability — and stays storage-agnostic: it is handed an already
resolved file :class:`~pathlib.Path`, so the domain subclass keeps ownership of
*where* its log lives (e.g. resolving a session directory through the workspace
store). The subclass supplies only the two domain facts the base cannot know:
how to serialize/parse a record and what its fold key is.

Records must expose ``to_json() -> str`` (one JSON object, no newline). Subclass
hooks: :meth:`_parse_record` (dict → record) and :meth:`_record_key`
(record → fold key).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, Iterable, Optional, Protocol, TypeVar, runtime_checkable

from mote.runtime.disk import disk_io
from mote.runtime.logging import logger


@runtime_checkable
class LedgerRecord(Protocol):
    """A ledger entry — must serialize to a single JSON object line."""

    def to_json(self) -> str:
        ...


R = TypeVar("R", bound=LedgerRecord)


class AppendOnlyLedger(ABC, Generic[R]):
    """Append-only JSONL, folded to the latest record per key, crash-durable.

    Cheap to construct: it folds any existing on-disk log into an in-memory
    latest-per-key index (so lookups are O(1)). All disk I/O is best-effort — a
    write failure logs and is swallowed so it can never break the caller; only
    cross-crash durability is lost, the in-memory index stays correct for the
    live process.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._latest: dict[str, R] = {}
        self._load()

    @property
    def path(self) -> Path:
        """The resolved JSONL file backing this ledger."""
        return self._path

    # ------------------------------------------------------------------
    # Subclass hooks — the only domain knowledge the base needs
    # ------------------------------------------------------------------

    @abstractmethod
    def _parse_record(self, data: dict) -> R:
        """Build a record from one decoded JSON object (raises on bad shape)."""

    @abstractmethod
    def _record_key(self, record: R) -> str:
        """The stable fold key for *record* (latest write per key wins)."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[R]:
        """The latest record for *key*, or ``None`` if never recorded."""
        return self._latest.get(key)

    def records(self) -> list[R]:
        """All folded records (latest per key), in insertion order."""
        return list(self._latest.values())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, record: R) -> None:
        """Fold *record* into the index and durably append it (fsync)."""
        self._latest[self._record_key(record)] = record
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            disk_io.append_line(self._path, record.to_json(), fsync=True)
        except OSError as e:
            logger.warning(f"Failed to append ledger record {self._path}: {e}")

    def reap(self, keys: Iterable[str]) -> None:
        """Drop the given keys and atomically rewrite the folded log.

        Keeps the file from growing without bound over a long-lived session
        once records have been resolved and no longer need to survive a crash.
        """
        ids = set(keys)
        if not ids:
            return
        removed = False
        for key in ids:
            if self._latest.pop(key, None) is not None:
                removed = True
        if removed:
            self._rewrite()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rewrite(self) -> None:
        try:
            lines = "".join(f"{r.to_json()}\n" for r in self._latest.values())
            disk_io.atomic_write(self._path, lines.encode("utf-8"), fsync=True)
        except OSError as e:
            logger.warning(f"Failed to rewrite ledger {self._path}: {e}")

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"Failed to read ledger {self._path}: {e}")
            return
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = self._parse_record(json.loads(raw))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue  # skip a torn/garbled line, keep folding the rest
            self._latest[self._record_key(record)] = record


__all__ = ["AppendOnlyLedger", "LedgerRecord"]
