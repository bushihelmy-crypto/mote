"""
AgentRegistry — global registry for spawnable agent types.

Usage:
    from metagpt.executor.agent_registry import register_agent, registry

    @register_agent
    class ExploreAgent(BaseAgent, Role):
        agent_name = "ExploreAgent"
        aliases = ["Explore"]
        description = "Fast codebase search."

    # Lookup (only Agent tool should do this)
    cls = registry.get("ExploreAgent")

Agent subclasses themselves should not access the registry. Agent types are
Role + BaseAgent subclasses that wear @register_agent and live under the
metagpt.agents package; discover() imports them so registration is automatic.
An agent owns its own schema via BaseAgent.get_schema — the registry only
registers and looks up.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import ClassVar

from metagpt.common.base.singleton import Singleton


class AgentRegistry(metaclass=Singleton):
    """Singleton registry for spawnable agent types (Role subclasses)."""

    _discovered: ClassVar[bool] = False

    def __init__(self):
        self._registry = {}

    def discover(self, package: str = "metagpt.roles.agents") -> None:
        """Recursively import every module under `package` so each @register_agent runs.

        This is what makes the registry pattern self-contained: an agent type
        registers simply by living under the package and wearing @register_agent —
        no central import list to maintain. Modules that fail to import are
        skipped. Idempotent; safe to call repeatedly.
        """
        if AgentRegistry._discovered:
            return
        AgentRegistry._discovered = True
        try:
            pkg = importlib.import_module(package)
        except Exception:  # noqa: BLE001
            return
        for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
            try:
                importlib.import_module(mod.name)
            except Exception:  # noqa: BLE001 — best-effort scan; skip unimportable modules
                continue

    def register(self, cls):
        """Class decorator that registers an agent type (a Role + BaseAgent subclass).

        - agent_name: uses cls.agent_name if set, otherwise cls.__name__ (lookup key)
        - aliases: registers additional lookup names if provided

        Enforces the spawn contract: a registered agent must be a Role subclass
        (so it can actually run). Names/aliases must not collide with a different
        already-registered agent. Schema/description are the agent's own
        responsibility (BaseAgent.get_schema) — not here.
        """
        # Contract: spawnable agents must be runnable Roles. Catch a missing
        # `Role` base at registration time instead of at spawn time. Imported
        # lazily to avoid an import cycle (role -> ... -> agents).
        from metagpt.roles.role import Role

        if not issubclass(cls, Role):
            raise TypeError(
                f"@register_agent: '{cls.__name__}' must subclass Role to be spawnable "
                f"(expected `class {cls.__name__}(BaseAgent, Role)`)."
            )

        name = getattr(cls, "agent_name", "") or cls.__name__
        cls.agent_name = name

        self._check_conflict(name, cls)
        self._registry[name] = cls
        for alias in getattr(cls, "aliases", []):
            self._check_conflict(alias, cls)
            self._registry[alias] = cls

        return cls

    def _check_conflict(self, key: str, cls) -> None:
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

    def all_agents(self) -> dict[str, type]:
        """Return all registered agent classes (deduplicated, primary name only)."""
        seen = {}
        for name, agent_cls in self._registry.items():
            if agent_cls not in seen.values():
                seen[agent_cls.agent_name] = agent_cls
        return seen


# Singleton instance
registry = AgentRegistry()

# Convenience alias — usage: @register_agent
register_agent = registry.register
