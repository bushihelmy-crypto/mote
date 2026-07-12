"""Concrete read-only child-Role builder + self-registration.

This is the ``roles``-layer implementation of the common-layer
:class:`~mote.common.interface.ChildRoleBuilder` contract. It constructs the
read-only, bypass-permission child :class:`Role` the code-review pipeline (and
any other ``executor`` caller) needs, then registers itself into the common
holder at import time so the executor can build such a child *without* importing
the ``roles`` stack.

Imported by ``mote.roles`` (the package ``__init__``), so the registration
fires whenever the roles layer is loaded — which, in production, is always
before any child agent is built (the pipeline runs inside a live agent).
"""

from __future__ import annotations

from typing import List, Optional

from mote.common.interface.child_role import register_child_role_builder
from mote.common.schema.permission_config import PermissionConfig
from mote.roles.role import Role
from mote.roles.role_schema import RoleSchema
from mote.roles.role_state import RoleState


def build_child_role(
    *,
    name: str,
    system_prompt: str,
    repo_dir: str = "",
    parent_session_id: str = "",
    tools: Optional[List[str]] = None,
) -> Role:
    """Construct a read-only, bypass-permission child Role.

    Args:
        name: Role name / profile (logging).
        system_prompt: The system prompt fixing the agent's task + output shape.
        repo_dir: Working directory; also seeds ``original_working_dir``.
        parent_session_id: Lineage link for the child's session.
        tools: Tool allow-list. Defaults to read-only investigation tools
            (``Read``/``Grep``/``Glob``); pass ``[]`` for a tool-less agent
            (e.g. the self-critique step, which only reasons over given text).

    Returns:
        An unstarted :class:`Role`.
    """
    schema = RoleSchema(
        name=name,
        profile=name,
        command_protocol="native",
        tools=["Read", "Grep", "Glob"] if tools is None else list(tools),
        permissions=PermissionConfig(mode="bypass"),
        system_prompt=system_prompt,
        enable_memory=False,
        # No durable session artifacts for these ephemeral leaf agents.
        record_file_history=False,
        record_terminal_state=False,
        record_kernel_state=False,
        record_browser_state=False,
    )
    state = RoleState(parent_session_id=parent_session_id or "")
    if repo_dir:
        state.working_dir = repo_dir
        state.original_working_dir = repo_dir
    return Role(role_schema=schema, state=state)


register_child_role_builder(build_child_role)


__all__ = ["build_child_role"]
