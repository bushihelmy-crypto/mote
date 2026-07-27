"""Runtime Temporal data converter — reuse pydantic's, do NOT hand-roll one.

Every value that crosses a Temporal boundary (activity args/return, workflow
args, signals) must be serializable by the client's data converter. mote's
durable payloads are pydantic-friendly — :class:`ThinkResult` / :class:`Message`
are ``BaseModel``\\s and :class:`ToolResult` is a stdlib ``@dataclass`` — all of
which ``temporalio.contrib.pydantic.pydantic_data_converter`` already handles
(it wraps pydantic's ``TypeAdapter`` machinery, covering ``BaseModel``,
dataclasses, and the stdlib types on top of Temporal's default JSON payloads).

So there is nothing to build here: this module just re-exports the ready-made
converter as the ONE mote-blessed converter, exactly as pydantic-ai's Temporal
integration does. Importing this module requires ``temporalio`` (the ``[temporal]``
extra); when it is absent the import fails and the backend factory degrades to
the JSONL tier.
"""

from __future__ import annotations

from temporalio.contrib.pydantic import pydantic_data_converter

#: The single mote-blessed Temporal data converter. Wire it into ``Client.connect``
#: (and any worker) so activity/workflow/signal payloads round-trip mote's
#: pydantic + dataclass result types without a custom converter.
data_converter = pydantic_data_converter

__all__ = ["data_converter", "pydantic_data_converter"]
