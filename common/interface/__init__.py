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
from metagpt.common.interface.event_subscriber import EventSubscriber
from metagpt.common.interface.file_snapshot import FileSnapshotStore
from metagpt.common.interface.hook_runner import HookRunner
from metagpt.common.interface.llm_client import LLMClient
from metagpt.common.interface.message_activity import MessageActivity
from metagpt.common.interface.message_sink import MessageSink
from metagpt.common.interface.message_store import MessageStore
from metagpt.common.interface.request_assembler import RequestAssembler
from metagpt.common.interface.turn_context import EphemeralContextSource

__all__ = [
    "MessageStore",
    "RequestAssembler",
    "LLMClient",
    "BackgroundPool",
    "MessageActivity",
    "MessageSink",
    "HookRunner",
    "FileSnapshotStore",
    "EphemeralContextSource",
    "EventSubscriber",
]
