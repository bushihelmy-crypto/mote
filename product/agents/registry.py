"""Product-owned Python Agent declaration collection.

Usage:
    from mote.product.agents.registry import register_agent

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

from mote.contracts.agent import BaseAgent
from mote.product.agents.catalog import AgentCatalog
from mote.runtime.agent.base import BaseRole


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

    def snapshot(self, factory) -> AgentCatalog[str]:
        """Freeze imported declarations into an isolated Application view."""

        return AgentCatalog.from_types(self.all_agents().values(), factory)


# Default catalog targeted by dynamically loaded Agent definitions.
registry = AgentRegistry()

# Convenience alias — usage: @register_agent
register_agent = registry.register


def declared_agent_catalog(factory) -> AgentCatalog[str]:
    """Freeze all Python Agent declarations imported in this process."""

    return registry.snapshot(factory)


__all__ = ["AgentRegistry", "declared_agent_catalog", "register_agent"]
