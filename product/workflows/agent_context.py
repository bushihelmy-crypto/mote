"""Product turn binding for the per-Agent durable Workflow capability."""

from contextvars import ContextVar, Token

from mote.product.workflows.agent_service import AgentWorkflowService

_ACTIVE_AGENT_WORKFLOWS: ContextVar[AgentWorkflowService | None] = ContextVar(
    "mote_active_agent_workflows", default=None
)


def bind_agent_workflows(
    service: AgentWorkflowService,
) -> Token[AgentWorkflowService | None]:
    return _ACTIVE_AGENT_WORKFLOWS.set(service)


def reset_agent_workflows(token: Token[AgentWorkflowService | None]) -> None:
    _ACTIVE_AGENT_WORKFLOWS.reset(token)


def resolve_agent_workflows() -> AgentWorkflowService:
    service = _ACTIVE_AGENT_WORKFLOWS.get()
    if service is None:
        raise RuntimeError("Agent Workflow capability is not bound to this turn")
    return service


__all__ = ["bind_agent_workflows", "reset_agent_workflows", "resolve_agent_workflows"]
