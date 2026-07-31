"""Per-run context and budget decisions consumed by execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mote.contracts.conversation import MessageQueue


@dataclass
class ExecutionContext:
    name: str
    display_name: str
    tools: list[str] = field(default_factory=list)
    msg_buffer: "MessageQueue | None" = None
    watch: set[str] = field(default_factory=set)
    enable_memory: bool = True
    observe_all: bool = True


@dataclass(frozen=True)
class BudgetVerdict:
    stop: bool = False
    message: str = ""


PROCEED = BudgetVerdict()


__all__ = ["BudgetVerdict", "ExecutionContext", "PROCEED"]
