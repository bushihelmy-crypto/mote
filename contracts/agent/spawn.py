"""Stable child-Agent spawn request contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Generic, Optional, Protocol, TypeVar, runtime_checkable

from mote.contracts.conversation import Message
from mote.contracts.output import RunOutcome

if TYPE_CHECKING:
    from mote.contracts.ports.agent.control import AgentControlPort

OutputT = TypeVar("OutputT")
BuilderOutputT = TypeVar("BuilderOutputT")
RequestT_contra = TypeVar("RequestT_contra", contravariant=True)
ChildOutputT = TypeVar("ChildOutputT")


class Lifecycle(Enum):
    MANAGED = "managed"
    EPHEMERAL = "ephemeral"


class ContextPolicy(Enum):
    FRESH = "fresh"
    SHARE_PARENT = "share_parent"


@dataclass
class SpawnContext:
    parent_id: Optional[str] = None
    agent_path: Optional[str] = None
    cwd: Optional[str] = None
    parent_session_id: str = ""


class CostAttributionPort(Protocol):
    """Read-only cost facts needed by the orchestration mirror tree."""

    def attributed_cost_usd(self) -> float: ...

    def attributed_total_tokens(self) -> int: ...

    def attributed_cost_is_estimated(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class AgentConstructionRequest:
    logical_agent_id: str
    parent_session_id: str | None
    child_identity: str
    child_path: str
    nickname: str
    cwd: str | None
    context_policy: ContextPolicy
    spawn_context: SpawnContext


class AgentBuilder(Protocol[RequestT_contra, BuilderOutputT]):
    def build(self, request: RequestT_contra) -> "RunnableAgent[BuilderOutputT]": ...


@runtime_checkable
class RunnableAgent(Protocol[OutputT]):
    """Stable execution/lifecycle surface returned by child builders."""

    @property
    def session_id(self) -> str: ...

    async def run(self, with_message: Message | None = None) -> RunOutcome[OutputT] | None: ...

    async def cleanup(self) -> None: ...

    def build_child_spawn_context(self, *, parent_id: str | None, agent_path: str) -> SpawnContext: ...

    def provision_spawned_child(self, child: "RunnableAgent[ChildOutputT]", policy: ContextPolicy) -> None: ...

    def provision_unparented_spawn(self, spawn_context: SpawnContext) -> None: ...

    def spawn_cost_attribution(self) -> CostAttributionPort: ...

    @property
    def state(self) -> "AgentRuntimeState": ...


class MessageBuffer(Protocol):
    def empty(self) -> bool: ...


class AgentRuntimeState(Protocol):
    @property
    def msg_buffer(self) -> MessageBuffer: ...


@dataclass(frozen=True, slots=True)
class SpawnableAgentDefinition(Generic[OutputT]):
    name: str
    aliases: tuple[str, ...]
    description: str
    version: str
    builder: AgentBuilder[AgentConstructionRequest, OutputT]


@dataclass
class SpawnPlan(Generic[OutputT]):
    request_id: str
    definition: SpawnableAgentDefinition[OutputT]
    nickname: Optional[str] = None
    parent_id: Optional[str] = None
    lifecycle: Lifecycle = Lifecycle.EPHEMERAL
    cost_rollup: bool = True
    watch_completion: bool = True
    max_depth: Optional[int] = None
    timeout_seconds: Optional[float] = None
    agent_role: str = ""
    context_policy: ContextPolicy = ContextPolicy.FRESH


__all__ = [
    "AgentBuilder",
    "AgentConstructionRequest",
    "CostAttributionPort",
    "ContextPolicy",
    "Lifecycle",
    "RunnableAgent",
    "SpawnContext",
    "SpawnPlan",
    "SpawnableAgentDefinition",
]
