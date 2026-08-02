"""Backend dispatch — turn a :class:`DurableConfig` into a live backend.

The single point that maps ``DurableConfig.backend`` onto a concrete
:class:`~mote.runtime.durable.backend.DurableBackend`, so the flow builder never
branches on the backend name itself:

* ``"jsonl"`` (default, zero-dependency) → :class:`JsonlBackend` over the shared
  run journal. This path imports nothing optional.
Temporal is intentionally not a ``DurableBackend`` variant: Product application
composition owns its client, worker and typed Workflow-effect plane.
"""

from __future__ import annotations

from mote.contracts.config.tool import DurableConfig
from mote.runtime.durable.backend import DurableBackend, JsonlBackend
from mote.runtime.ledger import RunJournal


def make_durable_backend(config: DurableConfig, journal: RunJournal) -> DurableBackend:
    """Build the durable backend selected by *config* over *journal*.

    The Runtime inference checkpoint seam is local JSONL only. A Temporal choice
    must be consumed by the Product Workflow application owner.
    """
    if config.backend == "temporal":
        raise RuntimeError("Temporal is activated only by the Product Workflow application owner")
    return JsonlBackend(journal)


__all__ = ["make_durable_backend"]
