"""Immutable runtime view of spawnable Agent definitions."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

from mote.contracts.agent import AgentConstructionRequest, BaseAgent, RunnableAgent, SpawnableAgentDefinition
from mote.contracts.ports.agent.factory import AgentFactory
from mote.runtime.agent.base import BaseRole

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class _ClassBoundBuilder:
    factory: AgentFactory
    agent_type: type[BaseAgent]

    def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]:
        return self.factory.child_builder(self.agent_type).build(request)


@dataclass(frozen=True, slots=True)
class AgentCatalog(Generic[OutputT]):
    """Immutable, versioned snapshot consumed by the spawn control plane."""

    version: str
    _definitions: tuple[SpawnableAgentDefinition[OutputT], ...]

    @classmethod
    def from_types(cls, types: Iterable[type[BaseAgent]], factory: AgentFactory) -> "AgentCatalog[str]":
        unique: dict[str, type[BaseAgent]] = {}
        aliases: dict[str, str] = {}
        for agent_type in types:
            if not issubclass(agent_type, BaseRole):
                raise TypeError(f"agent catalog entry '{agent_type.__name__}' must subclass BaseRole")
            name = getattr(agent_type, "agent_name", "") or agent_type.__name__
            existing = unique.get(name)
            if existing is not None and existing is not agent_type:
                raise ValueError(f"agent name '{name}' is declared more than once")
            unique[name] = agent_type
            for alias in getattr(agent_type, "aliases", ()):
                owner = aliases.get(alias)
                if owner is not None and owner != name:
                    raise ValueError(f"agent alias '{alias}' belongs to both '{owner}' and '{name}'")
                aliases[alias] = name
        ordered = tuple(unique[name] for name in sorted(unique))
        identity = "\n".join(_agent_type_identity(agent_type) for agent_type in ordered)
        version = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return AgentCatalog(
            version=version,
            _definitions=tuple(
                SpawnableAgentDefinition(
                    name=agent_type.agent_name,
                    aliases=tuple(agent_type.aliases),
                    description=(agent_type.description.strip() or (agent_type.__doc__ or "").strip()),
                    version=agent_type.definition_version,
                    builder=_ClassBoundBuilder(factory, agent_type),
                )
                for agent_type in ordered
            ),
        )

    def get(self, name: str) -> SpawnableAgentDefinition[OutputT] | None:
        for definition in self._definitions:
            if name == definition.name or name in definition.aliases:
                return definition
        return None

    def agent_type(self, name: str) -> type[BaseAgent] | None:
        """Product-only root-construction metadata; never exposed downstream."""

        definition = self.get(name)
        if definition is None or not isinstance(definition.builder, _ClassBoundBuilder):
            return None
        return definition.builder.agent_type

    def all_agents(self) -> dict[str, SpawnableAgentDefinition[OutputT]]:
        return {definition.name: definition for definition in self._definitions}

    def declared_types(self) -> tuple[type[BaseAgent], ...]:
        """Return Product-private declarations for immutable recomposition."""

        return tuple(
            definition.builder.agent_type
            for definition in self._definitions
            if isinstance(definition.builder, _ClassBoundBuilder)
        )

    def with_types(
        self: "AgentCatalog[str]",
        types: Iterable[type[BaseAgent]],
        factory: AgentFactory,
    ) -> "AgentCatalog[str]":
        """Return a new snapshot containing existing and newly declared agents."""

        additions = AgentCatalog.from_types(types, factory)
        definitions: list[SpawnableAgentDefinition[str]] = [*self._definitions]
        owners = {key: definition.name for definition in definitions for key in (definition.name, *definition.aliases)}
        for definition in additions._definitions:
            for key in (definition.name, *definition.aliases):
                owner = owners.get(key)
                if owner is not None:
                    raise ValueError(f"agent name or alias '{key}' conflicts with '{owner}'")
            definitions.append(definition)
            for key in (definition.name, *definition.aliases):
                owners[key] = definition.name
        identity = "\n".join(
            f"{definition.name}:{definition.version}" for definition in sorted(definitions, key=lambda item: item.name)
        )
        return AgentCatalog(
            version=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
            _definitions=tuple(definitions),
        )


def _agent_type_identity(agent_type: type[BaseAgent]) -> str:
    try:
        source = inspect.getsource(agent_type)
    except (OSError, TypeError):
        source = ""
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return ":".join(
        (
            getattr(agent_type, "agent_name", "") or agent_type.__name__,
            str(getattr(agent_type, "definition_version", "1")),
            source_digest,
        )
    )


__all__ = ["AgentCatalog"]
