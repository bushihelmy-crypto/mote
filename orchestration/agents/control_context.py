"""Turn-scoped binding of canonical Agent authority for workflow adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mote.contracts.ports.workflow.turn_control import WorkflowAgentTurnControlPort

_ACTIVE: ContextVar[WorkflowAgentTurnControlPort | None] = ContextVar("mote_workflow_caller_control", default=None)


@contextmanager
def bind_workflow_caller_control(control: WorkflowAgentTurnControlPort) -> Iterator[WorkflowAgentTurnControlPort]:
    token = _ACTIVE.set(control)
    try:
        yield control
    finally:
        _ACTIVE.reset(token)


def resolve_workflow_caller_control() -> WorkflowAgentTurnControlPort:
    control = _ACTIVE.get()
    if control is None:
        raise RuntimeError("Workflow caller control is not bound to this turn")
    return control
