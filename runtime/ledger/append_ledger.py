"""Fail-closed crash-durable, fold-by-key JSONL ledger primitive."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Iterable, Optional, Protocol, TypeVar, runtime_checkable

from mote.runtime.persistence import disk_io


@runtime_checkable
class LedgerRecord(Protocol):
    """A ledger entry serialized as one canonical JSON object line."""

    def to_json(self) -> str: ...


R = TypeVar("R", bound=LedgerRecord)


@dataclass(frozen=True)
class LedgerCommitReceipt:
    """Proof that one local ledger mutation reached its durable commit point."""

    record_key: str
    path: Path


class LedgerPersistenceError(RuntimeError):
    """A local ledger mutation did not reach its durable commit point."""

    def __init__(self, operation: str, path: Path, cause: OSError) -> None:
        self.operation = operation
        self.path = path
        self.cause = cause
        super().__init__(f"[ledger_persistence_failed] operation={operation} path={path}: {cause}")


class LedgerCorruptionError(RuntimeError):
    """A committed JSONL frame cannot be decoded or violates ledger invariants."""

    def __init__(self, path: Path, line_number: int, byte_offset: int, reason: str) -> None:
        self.path = path
        self.line_number = line_number
        self.byte_offset = byte_offset
        self.reason = reason
        super().__init__(f"[ledger_corruption] path={path} line={line_number} offset={byte_offset}: {reason}")


class AppendOnlyLedger(ABC, Generic[R]):
    """JSONL ledger whose observable state changes only after durable commit.

    A final byte segment without a newline is an uncommitted torn tail and is
    ignored in its entirety. Every newline-terminated frame is committed and
    therefore decoded strictly; corruption never degrades to an empty or
    partially folded ledger.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._latest: dict[str, R] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @abstractmethod
    def _parse_record(self, data: dict[str, object]) -> R:
        """Build a record from one decoded JSON object."""

    @abstractmethod
    def _record_key(self, record: R) -> str:
        """Return the stable fold key for *record*."""

    def _validate_transition(self, previous: R | None, record: R) -> None:
        """Validate a domain lifecycle transition before it becomes visible."""

    def get(self, key: str) -> Optional[R]:
        return self._latest.get(key)

    def records(self) -> list[R]:
        return list(self._latest.values())

    def append(self, record: R) -> LedgerCommitReceipt:
        """Durably append *record*, then publish it to the in-memory fold."""
        line, canonical = self._serialize_and_parse(record)
        key = self._record_key(canonical)
        self._validate_transition(self._latest.get(key), canonical)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            disk_io.append_line(self._path, line, fsync=True)
        except OSError as exc:
            raise LedgerPersistenceError("append", self._path, exc) from exc
        self._latest[key] = canonical
        return LedgerCommitReceipt(record_key=key, path=self._path)

    def reap(self, keys: Iterable[str]) -> LedgerCommitReceipt | None:
        """Durably replace the folded log, then publish the new snapshot."""
        ids = set(keys)
        if not ids:
            return None
        candidate = {key: record for key, record in self._latest.items() if key not in ids}
        if len(candidate) == len(self._latest):
            return None
        lines = "".join(f"{self._serialize_record(record)}\n" for record in candidate.values())
        try:
            disk_io.atomic_write(self._path, lines.encode("utf-8"), fsync=True)
        except OSError as exc:
            raise LedgerPersistenceError("reap", self._path, exc) from exc
        self._latest = candidate
        return LedgerCommitReceipt(record_key="reap", path=self._path)

    def _serialize_record(self, record: R) -> str:
        line, _ = self._serialize_and_parse(record)
        return line

    def _serialize_and_parse(self, record: R) -> tuple[str, R]:
        line = record.to_json()
        if "\n" in line or "\r" in line:
            raise ValueError("ledger record must occupy exactly one JSONL frame")
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise TypeError("ledger record must encode a JSON object")
        canonical = self._parse_record(decoded)
        if canonical != record:
            raise ValueError("ledger record does not round-trip through its strict decoder")
        return line, canonical

    def _decode_frame(self, raw: bytes) -> R:
        text = raw.decode("utf-8")
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise TypeError("ledger frame must be a JSON object")
        return self._parse_record(decoded)

    def _load(self) -> None:
        try:
            content = self._path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerPersistenceError("read", self._path, exc) from exc

        candidate: dict[str, R] = {}
        offset = 0
        frames = content.splitlines(keepends=True)
        for index, framed in enumerate(frames, start=1):
            terminated = framed.endswith((b"\n", b"\r"))
            if not terminated:
                break  # the only tolerated state: one uncommitted torn tail
            raw = framed.rstrip(b"\r\n")
            try:
                if not raw:
                    raise ValueError("empty committed ledger frame")
                record = self._decode_frame(raw)
                key = self._record_key(record)
                self._validate_transition(candidate.get(key), record)
            except Exception as exc:
                if isinstance(exc, LedgerPersistenceError):
                    raise
                raise LedgerCorruptionError(self._path, index, offset, str(exc)) from exc
            candidate[key] = record
            offset += len(framed)
        self._latest = candidate


__all__ = [
    "AppendOnlyLedger",
    "LedgerCommitReceipt",
    "LedgerCorruptionError",
    "LedgerPersistenceError",
    "LedgerRecord",
]
