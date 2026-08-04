"""Minimal query needed by Agent ingress recovery."""

from typing import Protocol


class AgentIncarnationGenerationQuery(Protocol):
    def current_generation(self, agent_id: str) -> int: ...


__all__ = ["AgentIncarnationGenerationQuery"]
