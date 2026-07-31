"""Product-owned discovery of spawnable Agent definitions."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from mote.contracts.ports.agent.factory import AgentFactory
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.markdown_loader import discover_md_agents
from mote.product.agents.registry import declared_agent_catalog

_AGENT_PACKAGES = ("mote.product.agents",)
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


def builtin_agent_catalog(factory: AgentFactory, cwd: Path | None = None) -> AgentCatalog[str]:
    """Build one Application snapshot, with Python definitions taking precedence."""

    discover_agents()
    python_agents = declared_agent_catalog(factory)
    markdown_agents = discover_md_agents(cwd)
    if not markdown_agents:
        return python_agents
    python_names = python_agents.all_agents()
    markdown = AgentCatalog.from_types(
        (agent_type for name, agent_type in markdown_agents.items() if name not in python_names),
        factory,
    )
    return AgentCatalog(
        version=f"{python_agents.version}:{markdown.version}",
        _definitions=tuple((*python_agents.all_agents().values(), *markdown.all_agents().values())),
    )


__all__ = ["builtin_agent_catalog", "discover_agents"]
