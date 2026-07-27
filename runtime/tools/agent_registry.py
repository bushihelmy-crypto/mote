"""
Agent declaration collection and immutable Application catalogs.

Usage:
    from mote.runtime.tools.agent_registry import register_agent

    @register_agent
    class ExploreAgent(BaseAgent, Role):
        agent_name = "ExploreAgent"
        aliases = ["Explore"]
        description = "Fast codebase search."

Agent subclasses themselves should not access the declaration collector. Agent types are
Role + BaseAgent subclasses that wear @register_agent and live under the
mote.agents package; discover() imports them so registration is automatic.
An agent owns its own schema via BaseAgent.get_schema — the registry only
registers and looks up.
"""
from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass

from mote.contracts.agents import BaseAgent
from mote.runtime.agent.base import BaseRole


@dataclass(frozen=True, slots=True)
class AgentCatalog:
    """Immutable, versioned snapshot of spawnable Agent definitions."""

    version: str
    _types: tuple[type[BaseAgent], ...]

    @classmethod
    def from_types(cls, types: Iterable[type[BaseAgent]]) -> "AgentCatalog":
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
        return cls(version=version, _types=ordered)

    def get(self, name: str) -> type[BaseAgent] | None:
        for agent_type in self._types:
            if name == agent_type.agent_name or name in getattr(agent_type, "aliases", ()):
                return agent_type
        return None

    def all_agents(self) -> dict[str, type[BaseAgent]]:
        return {
            (getattr(agent_type, "agent_name", "") or agent_type.__name__): agent_type for agent_type in self._types
        }

    def with_types(self, *types: type[BaseAgent], replace: bool = False) -> "AgentCatalog":
        merged = self.all_agents()
        for agent_type in types:
            name = getattr(agent_type, "agent_name", "") or agent_type.__name__
            existing = merged.get(name)
            if existing is not None and existing is not agent_type and not replace:
                raise ValueError(f"agent name '{name}' already belongs to '{existing.__name__}'")
            merged[name] = agent_type
        return type(self).from_types(merged.values())


def _agent_type_identity(agent_type: type[BaseAgent]) -> str:
    try:
        source = inspect.getsource(agent_type)
    except (OSError, TypeError):
        source = ""
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return ":".join(
        (
            getattr(agent_type, "agent_name", "") or agent_type.__name__,
            agent_type.__module__,
            agent_type.__qualname__,
            str(getattr(agent_type, "definition_version", "1")),
            source_digest,
        )
    )


class AgentRegistry:
    """Isolated registry for spawnable agent types (Role subclasses)."""

    def __init__(self):
        self._registry: dict[str, type[BaseAgent]] = {}

    def register(self, cls: type[BaseAgent]) -> type[BaseAgent]:
        """Class decorator that registers an agent type (a Role + BaseAgent subclass).

        - agent_name: uses cls.agent_name if set, otherwise cls.__name__ (lookup key)
        - aliases: registers additional lookup names if provided

        Enforces the spawn contract: a registered agent must be a runnable role
        (a ``BaseRole`` subclass). Names/aliases must not collide with a different
        already-registered agent. Schema/description are the agent's own
        responsibility (BaseAgent.get_schema) — not here.
        """
        # Contract: spawnable agents must be runnable roles. Catch a missing role
        # base at registration time instead of at spawn time. We check against the
        # common-layer ``BaseRole`` (which `roles.Role` extends and which defines
        # the runnable contract: think/act/react/run/...), so the executor never
        # imports the concrete `roles` stack — keeping it a true leaf w.r.t. roles
        # at both import time and runtime, with no cycle (role -> ... -> agents ->
        # executor).
        if not issubclass(cls, BaseRole):
            raise TypeError(
                f"@register_agent: '{cls.__name__}' must subclass Role to be spawnable "
                f"(expected `class {cls.__name__}(BaseAgent, Role)`)."
            )

        name = getattr(cls, "agent_name", "") or cls.__name__
        setattr(cls, "agent_name", name)

        self._check_conflict(name, cls)
        self._registry[name] = cls
        for alias in getattr(cls, "aliases", []):
            self._check_conflict(alias, cls)
            self._registry[alias] = cls

        return cls

    def _check_conflict(self, key: str, cls: type[BaseAgent]) -> None:
        """Reject a name/alias already taken by a *different* agent class.

        Re-registering the same class under the same key is idempotent (allowed),
        e.g. when discover() re-imports a module.
        """
        existing = self._registry.get(key)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"@register_agent: name '{key}' already registered to "
                f"'{existing.__name__}', cannot reassign to '{cls.__name__}'."
            )

    def get(self, name: str) -> type | None:
        """Look up a registered agent class by name or alias."""
        return self._registry.get(name)

    def all_agents(self) -> dict[str, type[BaseAgent]]:
        """Return all registered agent classes (deduplicated, primary name only)."""
        seen = {}
        for name, agent_cls in self._registry.items():
            if agent_cls not in seen.values():
                seen[agent_cls.agent_name] = agent_cls
        return seen

    def snapshot(self) -> AgentCatalog:
        """Freeze imported declarations into an isolated Application view."""

        return AgentCatalog.from_types(self.all_agents().values())


# Default catalog targeted by dynamically loaded Agent definitions.
registry = AgentRegistry()

# Convenience alias — usage: @register_agent
register_agent = registry.register


def declared_agent_catalog() -> AgentCatalog:
    """Freeze all Python Agent declarations imported in this process."""

    return registry.snapshot()


__all__ = ["AgentCatalog", "AgentRegistry", "declared_agent_catalog", "register_agent"]
