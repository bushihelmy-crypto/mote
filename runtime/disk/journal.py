"""Journal — a line-level append-only log (L2 of the persistence stack).

Generalizes the append-only-JSONL pattern that ``session/log.py`` grew for the
session rollout: a single file you append text lines to and later scan back,
tolerating partial/corrupt trailing lines. The session keeps its event
(de)serialization (``to_line``/``parse_line``); Journal only knows about *lines*.

Writes go through an explicitly owned :class:`~mote.runtime.disk.writer.DiskWriter`
(keyed by the journal's path) so appends to one journal are FIFO-ordered and a
``drain()`` barrier flushes them. ``create_if_absent`` is idempotent: it writes
the first line only when the file does not yet exist, so restart/resume never
re-writes a header or truncates history.

Leaf module: imports only ``common`` siblings + stdlib.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Union

import mote.runtime.disk.disk_io as disk_io
from mote.runtime.disk.writer import DiskWriter

PathLike = Union[str, Path]


class Journal:
    """An append-only text-line log file backed by an injected DiskWriter."""

    def __init__(self, path: PathLike, writer: DiskWriter | None = None):
        self._path = Path(path)
        self._key = str(self._path)
        self._writer = writer or DiskWriter()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def writer(self) -> DiskWriter:
        return self._writer

    def exists(self) -> bool:
        return self._path.exists()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def create_if_absent(self, first_line: str) -> bool:
        """Append ``first_line`` only when the journal does not yet exist.

        Returns True when the header was written, False when the file already
        existed (idempotent — restart/resume never re-writes it). The existence
        check runs synchronously so concurrent callers in the same process agree
        on who created it; the actual write is ordered through the DiskWriter.
        """
        if self._path.exists():
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch synchronously so a follow-up create_if_absent in the same process
        # sees the file and no-ops, even before the queued write lands.
        self._path.touch()
        self.append_line(first_line)
        return True

    def append_line(self, line: str) -> None:
        """Queue an append of one text ``line`` (FIFO per journal path)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        path = self._path
        self._writer.enqueue(self._key, lambda: disk_io.append_line(path, line))

    async def append_line_durable(self, line: str) -> None:
        """Append and await fsync completion, propagating any write failure."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        path = self._path
        await self._writer.submit(
            self._key,
            lambda: disk_io.append_line(path, line),
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def iter_raw_lines(self) -> Iterator[str]:
        """Yield each stored line (newline stripped), skipping blank lines.

        Tolerant of a torn trailing line from a crash mid-append: the caller's
        parser is expected to skip anything it cannot decode.
        """
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    yield line


__all__ = ["Journal"]
