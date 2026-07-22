"""Backend dispatch — turn a :class:`DurableConfig` into a live backend.

The single point that maps ``DurableConfig.backend`` onto a concrete
:class:`~mote.loop.durable.backend.DurableBackend`, so the loop's builder never
branches on the backend name itself:

* ``"jsonl"`` (default, zero-dependency) → :class:`JsonlBackend` over the shared
  run journal. This path imports nothing optional.
* ``"temporal"`` (opt-in) → the optional ``mote.durable_exec.temporal`` package's
  backend. That package — and ``temporalio`` — are imported **lazily, only on
  this branch**, so the core never carries the optional dependency. Two degrade
  paths keep a run durable rather than crashing: a missing ``[temporal]`` extra
  raises ``ImportError``; a not-yet-built backend (B0 lays the seam, B1-B4 fill
  it) raises ``NotImplementedError``. Either logs a clear warning and DEGRADES to
  the always-on JSONL tier (itself a valid durable backend). A missing/immature
  optional backend should never take an agent down — it falls back to the
  weaker-but-present tier, exactly like the rest of mote treats optional deps.

This module holds no Temporal knowledge beyond the module path it lazily imports,
so it stays a zero-dependency leaf that the core may import unconditionally.
"""

from __future__ import annotations

from mote.common.ledger import RunJournal
from mote.common.logs import logger
from mote.common.schema import DurableConfig
from mote.loop.durable.backend import DurableBackend, JsonlBackend


def make_durable_backend(config: DurableConfig, journal: RunJournal) -> DurableBackend:
    """Build the durable backend selected by *config* over *journal*.

    ``backend="jsonl"`` (or any degraded path) returns a :class:`JsonlBackend`.
    ``backend="temporal"`` lazily imports the optional package; a missing
    ``[temporal]`` extra logs a warning and falls back to the JSONL backend so a
    running agent keeps its always-on durability tier instead of crashing.
    """
    if config.backend == "temporal":
        try:
            from mote.durable_exec.temporal import make_temporal_backend

            return make_temporal_backend(config.temporal, journal)
        except ImportError as exc:
            logger.warning(
                "DurableConfig.backend='temporal' selected but the optional "
                f"'[temporal]' extra is not installed ({exc}); falling back to the "
                "always-on JSONL durable backend. Install with `pip install "
                "mote[temporal]` to enable the Temporal backend."
            )
        except NotImplementedError as exc:
            logger.warning(
                "DurableConfig.backend='temporal' selected but the Temporal "
                f"backend is not yet available ({exc}); falling back to the "
                "always-on JSONL durable backend."
            )
    return JsonlBackend(journal)


__all__ = ["make_durable_backend"]
