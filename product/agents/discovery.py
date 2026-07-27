"""Product-owned discovery of spawnable Agent definitions."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from mote.runtime.agent.agents.markdown_loader import discover_md_agents
from mote.runtime.tools.agent_registry import AgentCatalog, declared_agent_catalog

_AGENT_PACKAGES = ("mote.runtime.agent.agents",)
_agents_discovered = False


def discover_agents() -> None:
    """Import all configured Agent-definition modules exactly once."""
    global _agents_discovered
    if _agents_discovered:
        return
    for package in _AGENT_PACKAGES:
        module = importlib.import_module(package)
        for child in pkgutil.walk_packages(module.__path__, prefix=module.__name__ + "."):
            importlib.import_module(child.name)
    _agents_discovered = True


def builtin_agent_catalog(cwd: Path | None = None) -> AgentCatalog:
    """Build one Application snapshot, with Python definitions taking precedence."""

    discover_agents()
    python_agents = declared_agent_catalog()
    markdown_agents = discover_md_agents(cwd)
    available = python_agents.all_agents()
    available.update((name, agent_type) for name, agent_type in markdown_agents.items() if name not in available)
    return AgentCatalog.from_types(available.values())


__all__ = ["builtin_agent_catalog", "discover_agents"]
