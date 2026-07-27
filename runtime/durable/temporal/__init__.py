"""Tier-2 Temporal durable backend (opt-in, requires the ``[temporal]`` extra).

Reached only via ``mote.runtime.durable.make_durable_backend`` when
``DurableConfig.backend="temporal"``; importing this package pulls in
``temporalio`` (so a missing extra surfaces as an ``ImportError`` the factory
degrades on). The public entry is :func:`make_temporal_backend`.

The public entry is :func:`make_temporal_backend`, which builds a
:class:`TemporalBackend` — the Tier-2 :class:`DurableBackend` dispatching durable
steps as Temporal activities (memoized by event history) while driving the SAME
shared journal for EXTERNAL-effect idempotency. The client/worker lifecycle lives
in :mod:`.plugin`; the backend also runs steps inline without a worker, so a run
never loses durability just because no worker is attached.
"""

from __future__ import annotations

from mote.contracts.schema import TemporalConfig
from mote.runtime.durable.backend import DurableBackend
from mote.runtime.durable.temporal._activities import RUN_STEP_ACTIVITY, StepActivities, StepHandlerRegistry, StepInput
from mote.runtime.durable.temporal._backend import TemporalBackend
from mote.runtime.durable.temporal._converter import data_converter
from mote.runtime.durable.temporal.plugin import build_worker, connect_client
from mote.runtime.ledger import RunJournal


def make_temporal_backend(config: TemporalConfig, journal: RunJournal) -> DurableBackend:
    """Build the Temporal durable backend for *config* over *journal*.

    The seam the dispatch factory calls on the ``backend="temporal"`` branch:
    returns a :class:`TemporalBackend` (satisfies :class:`DurableBackend`) driving
    the shared *journal*. Importing this package pulls ``temporalio`` — a missing
    extra surfaces as an ``ImportError`` the factory degrades on; a successful
    import means the backend is live.
    """
    return TemporalBackend(config, journal)


__all__ = [
    "make_temporal_backend",
    "TemporalBackend",
    "StepActivities",
    "StepHandlerRegistry",
    "StepInput",
    "RUN_STEP_ACTIVITY",
    "data_converter",
    "connect_client",
    "build_worker",
]
