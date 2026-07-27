"""Temporal client / worker lifecycle for the Tier-2 durable backend.

The bootstrap half of Part B: connect a Temporal ``Client`` (with mote's pydantic
data converter so ``ThinkResult``/``Message``/``ToolResult`` serialize without a
custom converter) and build a ``Worker`` that registers the backend's generic
``run_step`` activity. Kept deliberately small and OPTIONAL — reached only when
``DurableConfig.backend="temporal"``; the core never imports any of this.

These helpers are thin wrappers over temporalio's own ``Client.connect`` /
``Worker`` so a host application can drive them, while the default JSONL tier
needs none of it. The backend's ``run_step`` also works INLINE without a running
worker (see :class:`TemporalBackend`), so a worker is only required to get the
distributed-durability + free-retry benefits of the Temporal tier.
"""

from __future__ import annotations

from typing import Optional, Sequence

try:
    from temporalio.client import Client
    from temporalio.worker import Worker
except ImportError:  # optional durable backend
    Client = None
    Worker = None

from mote.contracts.schema import TemporalConfig
from mote.runtime.durable.temporal._backend import TemporalBackend
from mote.runtime.durable.temporal._converter import data_converter


async def connect_client(config: TemporalConfig, **kwargs) -> "Client":
    """Connect a Temporal ``Client`` for *config* using mote's data converter.

    The pydantic data converter is applied unless the caller overrides it, so
    mote's pydantic models + stdlib dataclasses round-trip through Temporal
    payloads without a bespoke converter (matches pydantic-ai's approach).
    """
    if Client is None:
        raise RuntimeError("Temporal backend requires the 'temporalio' extra")
    kwargs.setdefault("data_converter", data_converter)
    return await Client.connect(
        config.server_address,
        namespace=config.namespace,
        **kwargs,
    )


def build_worker(
    client: "Client",
    backend: TemporalBackend,
    *,
    workflows: Optional[Sequence[type]] = None,
    **kwargs,
) -> "Worker":
    """Build a ``Worker`` on *config*'s task queue registering *backend*'s activity.

    Registers the ONE generic ``run_step`` activity the backend exposes; the host
    supplies its workflow class(es) via ``workflows``. Everything else (interceptors,
    concurrency limits) passes through as ``kwargs`` so the host keeps full control.
    """
    if Worker is None:
        raise RuntimeError("Temporal backend requires the 'temporalio' extra")
    return Worker(
        client,
        task_queue=backend.config.task_queue,
        workflows=list(workflows or []),
        activities=list(backend.temporal_activities),
        **kwargs,
    )


__all__ = ["connect_client", "build_worker", "data_converter"]
