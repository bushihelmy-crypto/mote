"""Canonical compiler for immutable spawnable-Agent catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Generic, Protocol, TypeVar

from mote.contracts.agent import AgentConstructionRequest, RunnableAgent, SpawnableAgentDefinition
from mote.contracts.ports.agent.factory import AgentFactory
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring

OutputT = TypeVar("OutputT")

_CATALOG_SCHEMA = "mote.agent-catalog/v1"


class SpawnableTextAgentClass(Protocol):
    __name__: str
    agent_name: ClassVar[str]
    aliases: ClassVar[list[str]]
    description: ClassVar[str]
    definition_version: ClassVar[str]
    definition_id: ClassVar[str]
    definition_source_path: ClassVar[str]
    definition_source_digest: ClassVar[str]

    def __call__(self, *, state: RoleState, wiring: AgentWiring[None, str]) -> RunnableAgent[str]: ...

    def get_schema(self) -> dict: ...


@dataclass(frozen=True, slots=True)
class _ClassBoundBuilder:
    factory: AgentFactory[SpawnableTextAgentClass]
    agent_type: SpawnableTextAgentClass

    def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]:
        return self.factory.child_builder(self.agent_type).build(request)


class AgentCatalog(Generic[OutputT]):
    """Immutable snapshot produced exclusively by :func:`compile_agent_catalog`."""

    __slots__ = ("_definitions", "_namespace", "version")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("AgentCatalog snapshots must be created by compile_agent_catalog()")

    @classmethod
    def _compiled(
        cls,
        *,
        version: str,
        definitions: tuple[SpawnableAgentDefinition[OutputT], ...],
        namespace: dict[str, SpawnableAgentDefinition[OutputT]],
    ) -> "AgentCatalog[OutputT]":
        catalog = object.__new__(cls)
        catalog.version = version
        catalog._definitions = definitions
        catalog._namespace = MappingProxyType(namespace)
        return catalog

    @classmethod
    def from_types(
        cls,
        types: Iterable[SpawnableTextAgentClass],
        factory: AgentFactory[SpawnableTextAgentClass],
    ) -> "AgentCatalog[str]":
        return compile_agent_catalog(_definition_from_type(agent_type, factory) for agent_type in types)

    def get(self, name: str) -> SpawnableAgentDefinition[OutputT] | None:
        return self._namespace.get(name)

    def agent_type(self, name: str) -> SpawnableTextAgentClass | None:
        """Product-only root-construction metadata; never exposed downstream."""

        definition = self.get(name)
        if definition is None or not isinstance(definition.builder, _ClassBoundBuilder):
            return None
        return definition.builder.agent_type

    def all_agents(self) -> dict[str, SpawnableAgentDefinition[OutputT]]:
        return {definition.name: definition for definition in self._definitions}

    def declared_types(self) -> tuple[SpawnableTextAgentClass, ...]:
        """Return Product-private declarations for immutable recomposition."""

        return tuple(
            definition.builder.agent_type
            for definition in self._definitions
            if isinstance(definition.builder, _ClassBoundBuilder)
        )

    def with_types(
        self: "AgentCatalog[str]",
        types: Iterable[SpawnableTextAgentClass],
        factory: AgentFactory[SpawnableTextAgentClass],
    ) -> "AgentCatalog[str]":
        """Compile a new snapshot from this snapshot and additional declarations."""

        additions = tuple(_definition_from_type(agent_type, factory) for agent_type in types)
        return compile_agent_catalog((*self._definitions, *additions))


def compile_agent_catalog(
    definitions: Iterable[SpawnableAgentDefinition[OutputT]],
) -> AgentCatalog[OutputT]:
    """Validate, canonically order, and content-address a complete catalog."""

    ordered = tuple(sorted(definitions, key=_definition_sort_key))
    namespace: dict[str, SpawnableAgentDefinition[OutputT]] = {}
    canonical_payload: list[dict[str, object]] = []
    for definition in ordered:
        aliases = tuple(sorted(definition.aliases))
        keys = (definition.name, *aliases)
        if not definition.name:
            raise ValueError("agent canonical name must not be empty")
        if not definition.version:
            raise ValueError(f"agent definition '{definition.name}' must have an identity")
        if any(not key for key in keys):
            raise ValueError(f"agent definition '{definition.name}' contains an empty alias")
        if len(set(keys)) != len(keys):
            raise ValueError(f"agent definition '{definition.name}' repeats a name or alias")
        for key in keys:
            owner = namespace.get(key)
            if owner is not None:
                raise ValueError(
                    f"agent name or alias '{key}' belongs to both " f"'{owner.name}' and '{definition.name}'"
                )
            namespace[key] = definition
        canonical_payload.append(
            {
                "aliases": aliases,
                "description": definition.description,
                "definition_id": definition.version,
                "name": definition.name,
            }
        )
    encoded = json.dumps(
        {"definitions": canonical_payload, "schema": _CATALOG_SCHEMA},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    version = f"sha256-{hashlib.sha256(encoded).hexdigest()}"
    return AgentCatalog._compiled(version=version, definitions=ordered, namespace=namespace)


def _definition_from_type(
    agent_type: SpawnableTextAgentClass,
    factory: AgentFactory[SpawnableTextAgentClass],
) -> SpawnableAgentDefinition[str]:
    name = agent_type.agent_name or agent_type.__name__
    return SpawnableAgentDefinition(
        name=name,
        aliases=tuple(agent_type.aliases),
        description=(agent_type.description.strip() or (agent_type.__doc__ or "").strip()),
        version=agent_type.definition_version,
        builder=_ClassBoundBuilder(factory, agent_type),
    )


def _definition_sort_key(definition: SpawnableAgentDefinition[OutputT]) -> tuple[str, tuple[str, ...], str, str]:
    return (
        definition.name,
        tuple(sorted(definition.aliases)),
        definition.description,
        definition.version,
    )


__all__ = ["AgentCatalog", "compile_agent_catalog"]
