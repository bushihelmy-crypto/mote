"""Discovery boundary for optional durable-execution backends."""
from __future__ import annotations

from importlib import import_module
from typing import Callable

from mote.common.ledger import RunJournal
from mote.common.schema import TemporalConfig
from mote.loop.durable.backend import DurableBackend

DurableBackendFactory = Callable[[TemporalConfig, RunJournal], DurableBackend]

_BACKEND_ENTRYPOINTS = {
    "temporal": ("mote.durable_exec.temporal", "make_temporal_backend"),
}


def load_backend_factory(name: str) -> DurableBackendFactory:
    """Load one registered optional backend factory by name."""
    try:
        module_name, attribute = _BACKEND_ENTRYPOINTS[name]
    except KeyError as exc:
        raise ValueError(f"unknown durable backend {name!r}") from exc
    module = import_module(module_name)
    return getattr(module, attribute)


__all__ = ["DurableBackendFactory", "load_backend_factory"]
