"""Narrow state and construction ports consumed by Agent Residency."""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from mote.contracts.content import ContentDigest
from mote.contracts.conversation import Message
from mote.contracts.events.envelope import JsonValue


@runtime_checkable
class ResidentAgentStatePort(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def residency_definition_id(self) -> str: ...

    @property
    def residency_config_digest(self) -> ContentDigest: ...

    def export_residency_state(self, *, session_history_is_durable: bool) -> Mapping[str, JsonValue]: ...

    def restore_residency_message_buffer(self, snapshot: JsonValue) -> None: ...

    def restore_residency_history(
        self,
        messages: tuple[Message, ...],
        session_meta: Mapping[str, object],
    ) -> None: ...


class ResidentAgentFactory(Protocol):
    @property
    def definition_id(self) -> str: ...

    @property
    def config_digest(self) -> ContentDigest: ...

    def build(self, state: Mapping[str, JsonValue]) -> ResidentAgentStatePort: ...


__all__ = ["ResidentAgentFactory", "ResidentAgentStatePort"]
