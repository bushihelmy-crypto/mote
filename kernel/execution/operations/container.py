"""Execution operation bundle used only at graph assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.operations.output import OutputOperation

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class GraphAssemblyInputs(Generic[OutputT]):
    """Composition-root inputs; never passed through to graph nodes."""

    context: Callable[[], ExecutionContext]
    observation: Any
    inference: Any
    actions: Any
    outputs: OutputOperation[OutputT]
    context_provider: Any
    completion_policy: Any
    current_channel: Callable[[], Any]
    inference_engine: Any
    set_active: Callable[[bool], None]
    get_bg_pool: Callable[[], Any]
    advance_turn: Callable[[], int] | None


__all__ = ["GraphAssemblyInputs"]
