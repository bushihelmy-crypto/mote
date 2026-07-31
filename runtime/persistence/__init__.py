"""Persistence primitives — the ordered disk-write execution layer.

Three minimal, composable layers beneath durable runtime state:

* **L0** :mod:`disk_io` — stateless file primitives (read/write/range/tail) plus
  the two durability primitives :func:`~disk_io.atomic_write` (tmp+fsync+replace)
  and :func:`~disk_io.append_line`.
* **L1** :class:`~writer.DiskWriter` — an explicitly owned serial async write
  queue with per-key FIFO and a ``drain()`` barrier.
* **L2** :class:`~journal.Journal` — a line-level append-only log built on the
  DiskWriter (generalizes the session rollout's append pattern).
"""

from mote.runtime.persistence import atomic as disk_io
from mote.runtime.persistence.atomic import (
    append_line,
    atomic_write,
    file_size,
    mtime_ns,
    mtime_seconds,
    read_range,
    read_tail,
    remove_file,
    remove_tree,
    truncate_file,
    write_bytes,
    write_capped,
)
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction
from mote.runtime.persistence.journal_writer import Journal
from mote.runtime.persistence.writer import DiskWriter

__all__ = [
    "disk_io",
    "atomic_write",
    "append_line",
    "write_bytes",
    "write_capped",
    "read_range",
    "read_tail",
    "file_size",
    "mtime_ns",
    "mtime_seconds",
    "truncate_file",
    "remove_file",
    "remove_tree",
    "DiskWriter",
    "Journal",
    "RuntimeExecutionTransaction",
]
