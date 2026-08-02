"""Narrow routing capability consumed by a runnable Agent."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.conversation import Message


class AgentRoutingPort(Protocol):
    def set_addresses(self, agent_id: str, addresses: set[str]) -> None: ...

    def publish_message(self, message: Message) -> None: ...


__all__ = ["AgentRoutingPort"]
