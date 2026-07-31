"""Product-owned discovery of spawnable Agent definitions."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.ports.agent.factory import AgentFactory
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.markdown_loader import discover_md_agents


def builtin_agent_catalog(factory: AgentFactory, cwd: Path | None = None) -> AgentCatalog[str]:
    """Build one Application snapshot, with Python definitions taking precedence."""

    python_agents = AgentCatalog.from_types((), factory)
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


__all__ = ["builtin_agent_catalog"]
