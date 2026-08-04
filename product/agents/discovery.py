"""Product-owned discovery of spawnable Agent definitions."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from mote.contracts.ports.agent.factory import AgentFactory
from mote.product.agents.catalog import AgentCatalog, SpawnableTextAgentClass
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
    # ``discover_md_agents`` constructs concrete ``BaseAgent + Role`` classes;
    # that builder is the runtime proof of the spawnable class protocol.
    validated = cast(tuple[SpawnableTextAgentClass, ...], tuple(markdown_agents.values()))
    return AgentCatalog.from_types(validated, factory)


__all__ = ["builtin_agent_catalog"]
