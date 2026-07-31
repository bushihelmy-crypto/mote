"""Layer-neutral view of the spawnable Agent catalog."""

from __future__ import annotations

from typing import Mapping, Protocol, TypeVar

from mote.contracts.agent import SpawnableAgentDefinition

OutputT = TypeVar("OutputT")


class SpawnableAgentCatalog(Protocol[OutputT]):
    @property
    def version(self) -> str:
        ...

    def get(self, name: str) -> SpawnableAgentDefinition[OutputT] | None:
        ...

    def all_agents(self) -> Mapping[str, SpawnableAgentDefinition[OutputT]]:
        ...


__all__ = ["SpawnableAgentCatalog"]
