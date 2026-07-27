"""Per-run context and budget decisions consumed by the agent flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mote.contracts.schema import MessageQueue


@dataclass
class FlowContext:
    """Narrow identity and observation inputs for one agent flow."""

    name: str
    display_name: str
    tools: list[str] = field(default_factory=list)
    msg_buffer: "MessageQueue | None" = None
    watch: set = field(default_factory=set)
    enable_memory: bool = True
    observe_all: bool = True


@dataclass(frozen=True)
class BudgetVerdict:
    """Budget gate decision made before a model request."""

    stop: bool = False
    message: str = ""


PROCEED = BudgetVerdict()


__all__ = ["BudgetVerdict", "FlowContext", "PROCEED"]
