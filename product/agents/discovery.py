"""Product-owned discovery of spawnable Agent definitions."""

from __future__ import annotations

from pathlib import Path

from mote.contracts.ports.agent.factory import AgentFactory
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.markdown_loader import discover_md_agents
from mote.product.extensions.sources import ExtensionSourcePolicy


def builtin_agent_catalog(
    factory: AgentFactory,
    cwd: Path | None,
    *,
    source_policy: ExtensionSourcePolicy,
) -> AgentCatalog[str]:
    """Compile the complete builtin namespace as one Application snapshot."""

    markdown_agents = discover_md_agents(cwd, source_policy=source_policy)
    return AgentCatalog.from_types(markdown_agents.values(), factory)


__all__ = ["builtin_agent_catalog"]
