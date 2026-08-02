"""Execution operation bundle used only at graph assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from mote.contracts.ports.execution.model_turn_completion import ModelTurnCompletionPolicy
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.kernel.commands import CommandChannel
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.context_provider import BaseContextProvider
from mote.kernel.execution.operations.action_execution import ActionExecutionService
from mote.kernel.execution.operations.inference import InferenceService
from mote.kernel.execution.operations.observation import ObservationService
from mote.kernel.execution.operations.output import OutputOperation
from mote.kernel.inference.base import BaseInferenceEngine

OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class GraphAssemblyInputs(Generic[OutputT]):
    """Composition-root inputs; never passed through to graph nodes."""

    context: Callable[[], ExecutionContext]
    observation: ObservationService
    inference: InferenceService
    actions: ActionExecutionService
    outputs: OutputOperation[OutputT]
    context_provider: BaseContextProvider
    completion_policy: ModelTurnCompletionPolicy
    current_channel: Callable[[], CommandChannel]
    inference_engine: BaseInferenceEngine
    set_active: Callable[[bool], None]
    get_bg_pool: Callable[[], BackgroundTaskService | None]
    advance_turn: Callable[[], int] | None


__all__ = ["GraphAssemblyInputs"]
