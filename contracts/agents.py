"""
BaseAgent — identity contract for spawnable agent types.

An agent type is a Role subclass that also inherits BaseAgent to carry its
spawn-time identity (agent_name/description/aliases) and to describe itself via
get_schema(). Registration is done with @register_agent; the registry only
registers and looks up — an agent owns its own schema, mirroring BaseTool.

Note: `agent_name` (not `name`) is used deliberately so it never shadows
Role.name (which returns role_schema.name at runtime).

Usage:
    @register_agent
    class ExploreAgent(BaseAgent, Role):
        agent_name = "ExploreAgent"
        aliases = ["Explore"]
        description = "Fast codebase search."
"""
from __future__ import annotations

from typing import ClassVar


class BaseAgent:
    """Identity + self-description for a spawnable agent type."""

    # --- Identity ---
    agent_name: ClassVar[str] = ""  # Primary agent type name (lookup key)
    aliases: ClassVar[list[str]] = []  # Alternative names (LLM can use any)
    description: ClassVar[str] = ""  # Override; if empty, taken from docstring
    definition_version: ClassVar[str] = "1"  # Catalog/recovery identity override.

    @classmethod
    def get_schema(cls) -> dict:
        """Compute this agent type's schema. An agent owns its own schema — the
        registry only registers and looks up, it does not describe.

        The first docstring line is the fallback description when `description`
        is not set.
        """
        description = cls.description.strip() or (cls.__doc__ or "").strip()
        return {
            "name": cls.agent_name,
            "description": description,
        }
