"""Structural interfaces (PEP 544 Protocols) for the react-loop collaborators.

These describe the *narrow slice* each consumer needs from a duck-typed
dependency, so the assembly site (Role) is statically checked: if an
implementation drifts from what the loop/channel/think-engine relies on, a
type checker flags it at the injection point instead of failing at runtime.

Why Protocol, not ABC: the collaborators have multiple unrelated implementations
(real ``ContextManager`` + ``FakeLLM`` in tests + provider clients) that must NOT
share a base class. Protocols are structural — anything shaped right conforms,
no inheritance required — which preserves the framework's duck-typing while
still giving static guarantees.

Interface segregation: a single object may satisfy several of these (e.g.
``ContextManager`` is both a ``MessageStore`` and a ``RequestAssembler``), but
each consumer depends only on the face it uses — mirroring the tool-capability
allowlist, extended from the tool<->Role boundary to component<->component edges.

This is a LEAF package: it imports only ``typing`` and (under TYPE_CHECKING) the
``Message`` type, so it can be imported from anywhere without risking a cycle.
"""

from metagpt.common.interface.background_pool import BackgroundPool
from metagpt.common.interface.browser_state import BrowserStateStore
from metagpt.common.interface.child_role import (
    ChildRoleBuilder,
    build_child_role,
    register_child_role_builder,
)
from metagpt.common.interface.context_reducer import ContextReducer
from metagpt.common.interface.event_subscriber import (
    DEFAULT_PRIORITY,
    DEFAULT_STAGE,
    DURABLE,
    FAIL_CLOSED,
    FAIL_OPEN,
    MIRROR,
    BusAware,
    ControlOutcome,
    ControlStage,
    ControlSubscriber,
    DeliveryPolicy,
    FailMode,
    ObservationSubscriber,
    ObserverPriority,
    SyncObserver,
)
from metagpt.common.interface.file_snapshot import FileSnapshotStore
from metagpt.common.interface.hook_runner import HookRunner
from metagpt.common.interface.kernel_state import KernelStateStore
from metagpt.common.interface.llm_client import LLMClient
from metagpt.common.interface.message_activity import MessageActivity
from metagpt.common.interface.message_sink import MessageSink
from metagpt.common.interface.message_store import MessageStore
from metagpt.common.interface.request_assembler import RequestAssembler
from metagpt.common.interface.resource_loader import ResourceProvider
from metagpt.common.interface.terminal_state import TerminalStateStore
from metagpt.common.interface.turn_context import (
    DEFAULT_TURN_CONTEXT_PRIORITY,
    EphemeralContextSource,
    TurnContextPriority,
)

__all__ = [
    "MessageStore",
    "RequestAssembler",
    "LLMClient",
    "BackgroundPool",
    "MessageActivity",
    "MessageSink",
    "HookRunner",
    "FileSnapshotStore",
    "ResourceProvider",
    "TerminalStateStore",
    "KernelStateStore",
    "BrowserStateStore",
    "ChildRoleBuilder",
    "build_child_role",
    "register_child_role_builder",
    "ContextReducer",
    "EphemeralContextSource",
    "TurnContextPriority",
    "DEFAULT_TURN_CONTEXT_PRIORITY",
    "ControlSubscriber",
    "ControlOutcome",
    "ControlStage",
    "DEFAULT_STAGE",
    "ObserverPriority",
    "DEFAULT_PRIORITY",
    "ObservationSubscriber",
    "SyncObserver",
    "BusAware",
    "DeliveryPolicy",
    "MIRROR",
    "DURABLE",
    "FailMode",
    "FAIL_OPEN",
    "FAIL_CLOSED",
]
