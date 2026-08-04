"""Optional Temporal mechanisms activated by the Product application owner.

Product composition imports this package only when Temporal is explicitly
selected. It owns one client, worker, frozen typed activity catalog, and Workflow
effect plane; Runtime neither selects handlers nor activates another worker.
"""

from __future__ import annotations

from mote.runtime.durable.temporal._activities import (
    RUN_STEP_ACTIVITY,
    StepActivities,
    StepInput,
    TemporalActivityCatalog,
)
from mote.runtime.durable.temporal._converter import data_converter
from mote.runtime.durable.temporal.plugin import build_worker, connect_client
from mote.runtime.durable.temporal.runtime import TemporalActivityRuntime

__all__ = [
    "TemporalActivityRuntime",
    "StepActivities",
    "TemporalActivityCatalog",
    "StepInput",
    "RUN_STEP_ACTIVITY",
    "data_converter",
    "connect_client",
    "build_worker",
]
