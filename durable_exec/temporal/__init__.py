"""Tier-2 Temporal durable backend (opt-in, requires the ``[temporal]`` extra).

Reached ONLY via ``mote.loop.durable.make_durable_backend`` when
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

from mote.common.ledger import RunJournal
from mote.common.schema import TemporalConfig
from mote.durable_exec.temporal._activities import RUN_STEP_ACTIVITY, StepActivities, StepHandlerRegistry, StepInput
from mote.durable_exec.temporal._backend import TemporalBackend
from mote.durable_exec.temporal._converter import data_converter
from mote.durable_exec.temporal.plugin import build_worker, connect_client
from mote.loop.durable.backend import DurableBackend


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
