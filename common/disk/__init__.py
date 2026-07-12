"""Persistence primitives — the ordered disk-write execution layer.

Three minimal, composable layers beneath the event-bus spine:

* **L0** :mod:`disk_io` — stateless file primitives (read/write/range/tail) plus
  the two durability primitives :func:`~disk_io.atomic_write` (tmp+fsync+replace)
  and :func:`~disk_io.append_line`.
* **L1** :class:`~writer.DiskWriter` — a single serial async write queue with
  per-key FIFO and a global ``drain()`` barrier (with a synchronous inline
  fallback when no event loop is running). Use :func:`~writer.get_disk_writer`.
* **L2** :class:`~journal.Journal` — a line-level append-only log built on the
  DiskWriter (generalizes the session rollout's append pattern).
"""

from metagpt.common.disk import disk_io
from metagpt.common.disk.disk_io import (
    append_line,
    atomic_write,
    file_size,
    read_range,
    read_tail,
    remove_file,
    truncate_file,
    write_bytes,
    write_capped,
)
from metagpt.common.disk.journal import Journal
from metagpt.common.disk.writer import (
    DiskWriter,
    drain_blocking,
    get_disk_writer,
    set_disk_writer,
)

__all__ = [
    "disk_io",
    "atomic_write",
    "append_line",
    "write_bytes",
    "write_capped",
    "read_range",
    "read_tail",
    "file_size",
    "truncate_file",
    "remove_file",
    "DiskWriter",
    "get_disk_writer",
    "set_disk_writer",
    "drain_blocking",
    "Journal",
]
