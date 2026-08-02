"""Turn-scoped discovery of canonical Agent authority for Workflow adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mote.contracts.ports.workflow.turn_control import WorkflowAgentTurnControlPort

_ACTIVE_WORKFLOW_CALLER_CONTROL: ContextVar[WorkflowAgentTurnControlPort | None] = ContextVar(
    "mote_workflow_caller_control", default=None
)


@contextmanager
def bind_workflow_caller_control(
    control: WorkflowAgentTurnControlPort,
) -> Iterator[WorkflowAgentTurnControlPort]:
    token = _ACTIVE_WORKFLOW_CALLER_CONTROL.set(control)
    try:
        yield control
    finally:
        _ACTIVE_WORKFLOW_CALLER_CONTROL.reset(token)


def resolve_workflow_caller_control() -> WorkflowAgentTurnControlPort:
    control = _ACTIVE_WORKFLOW_CALLER_CONTROL.get()
    if control is None:
        raise RuntimeError("Workflow caller control is not bound to this turn")
    return control


__all__ = ["bind_workflow_caller_control", "resolve_workflow_caller_control"]
