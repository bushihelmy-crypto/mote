#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``backend`` — the single binding seam onto the mote engine (Ports & Adapters).

This is the **only** module in ``mote.cli`` that imports mote's concrete
engine classes (Role / Runtime / Control / Context / Schema / State /
UserMessage / …). Everything else in the CLI (``app.py`` / ``driver.py``) reaches
the engine exclusively through the module-level functions here.

Deliberately **not** a Protocol / interface / IoC container. Following a mature
UI↔engine split, we bind with a flat set of concrete
functions + injected callables — the seam is a *place*, not an abstraction. When
the engine grows a new capability, one function lands here; the CLI stays blind
to the concrete types.

Two axes are collapsed onto this seam:

* **Bootstrap/construction** (``load_config`` / ``build_context`` / ``build_role``
  / ``build_control`` / ``wrap_runtime``) — the ``Config → Context → Role →
  Runtime → Control`` spine app.py used to own inline.
* **Accessors** (``bind_human_channel`` / ``runtime_name`` / ``fork_role`` /
  ``clear_messages`` / ``turn_message`` / …) — the scattered duck-typed pokes
  driver.py used to make into ``role.state`` / ``runtime.role.role_schema`` / etc.
  These are pure attribute operations, so a lightweight fake satisfies them too.

Also exposes the engine's own agent-type registry (``list_agent_types`` +
``build_role(agent_type=...)``) so the CLI can spawn typed agents without
building a parallel type system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

from mote.common.config.loader import load_config as _load_config
from mote.common.const import IMAGES
from mote.common.schema import UserMessage
from mote.common.schema.file_watch_config import FileWatchConfig
from mote.common.utils.git_state import find_git_root
from mote.environment.control import AgentControl
from mote.environment.runtime import AgentRuntime
from mote.executor.agent_md_loader import register_md_agents
from mote.executor.agent_registry import registry as agent_registry
from mote.executor.mcp.config_source import load_mcp_servers
from mote.executor.permission.settings_source import load_permission_rules
from mote.roles import Role
from mote.roles.role_schema import RoleSchema
from mote.roles.role_state import RoleState
from mote.router.llm.context import Context


# ======================================================================
# Bootstrap / construction
# ======================================================================
def load_config(model: Optional[str] = None) -> Any:
    """Load the engine config, optionally overriding the LLM model."""
    return _load_config(programmatic=({"llm__model": model} if model else None))


def build_context(config: Any) -> Any:
    """Build the engine :class:`Context` (opaque handle to app.py)."""
    return Context(config=config)


def _apply_cwd(role: Any, cwd: Optional[str]) -> None:
    """Point a freshly built role at *cwd* (git root becomes its project root)."""
    if not cwd:
        return
    role.state.working_dir = cwd
    role.state.original_working_dir = cwd
    role.state.project_root = find_git_root(cwd) or cwd


def _discover_mcps(cwd: Optional[str] = None) -> List[str]:
    """Every MCP server declared in ``.mote/mcp.json`` (empty when unconfigured).

    Mirrors the skill subsystem's "empty include list ⇒ load everything" default,
    but resolved *here* (the top-level interactive role) rather than in the engine
    so child agents — whose schema deliberately clears ``mcps`` (see
    ``roles.capabilities``) — keep their MCP-less isolation. Discovery walks from
    *cwd* up to the git root (plus ``~/.mote/mcp.json``); a missing / empty /
    malformed file yields ``[]``, so MCP simply stays off until the user drops a
    server block into the file (a change the file watcher then hot-reloads).
    """
    return [s.name for s in load_mcp_servers(Path(cwd) if cwd else None)]


def build_role(
    *,
    context: Any,
    name: str,
    tools: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[Any]:
    """Unified role factory (initial / new / resume / typed spawn).

    ``agent_type`` empty → the generic ``Role`` path (explicit schema + state).
    ``agent_type`` given → look it up in the engine's agent registry; an unknown
    type returns ``None`` (the caller surfaces the failure), otherwise the agent
    class self-configures its own schema/tools.

    The generic path is the interactive top-level role, so it opts into the two
    "watch the workspace" conveniences a human at a REPL expects: every MCP server
    in ``mcp_config.json`` is loaded (its tools surface in the per-turn catalog),
    and a file watcher hot-reloads MCP servers *and* skills when their config
    files change mid-session. **Tools**, by contrast, follow RoleSchema's curated
    default when no explicit ``tools`` are passed — so what the CLI reports (and
    what the agent is actually wired with) is exactly the declared set, not the
    full registered toolbox (which includes internal control verbs like
    End/Reply/Ask that are not part of the curated surface). Typed agents
    self-configure and are left untouched.

    Before any lookup, ``.mote/agents/*.md`` files (git-root walk from *cwd* plus
    ``~/.mote/agents``) are registered as spawnable agent types, so a markdown
    agent resolves for a typed spawn and surfaces in the Agent tool's catalog.
    """
    register_md_agents(Path(cwd) if cwd else None)
    if agent_type:
        agent_registry.discover()
        cls = agent_registry.get(agent_type)
        if cls is None:
            return None
        role = cls(context=context, name=name)
    else:
        schema_kwargs: dict = {
            "name": name,
            "mcps": _discover_mcps(cwd),
            "file_watch": FileWatchConfig(enabled=True, reload_mcp=True, reload_skills=True),
        }
        # An explicit --tools list wins; otherwise the field is left unset so the
        # RoleSchema curated default (its declared tool surface) applies — the CLI
        # then reports exactly that declared set, not the full registered toolbox.
        if tools:
            schema_kwargs["tools"] = list(tools)
        permissions = load_permission_rules(Path(cwd) if cwd else None)
        if permissions is not None:
            schema_kwargs["permissions"] = permissions
        schema = RoleSchema(**schema_kwargs)
        state = RoleState(session_id=session_id) if session_id else RoleState()
        role = Role(name=name, role_schema=schema, state=state, context=context)
    _apply_cwd(role, cwd)
    return role


def build_control(role: Any) -> Tuple[Any, Any]:
    """Build the control plane, adopt *role* as the root, wire it into its context.

    Returns ``(control, root_runtime)``. Also writes ``context.agent_control`` so
    spawn sites holding the Context reach the live plane directly.
    """
    control = AgentControl(session_id=role.session_id)
    runtime = wrap_runtime(role)
    control.add_agent(runtime, root=True)
    role.context.agent_control = control
    return control, runtime


def wrap_runtime(role: Any) -> Any:
    """Wrap a role into a runtime for the control plane."""
    return AgentRuntime(role)


# ======================================================================
# Accessors (pure attribute operations — fakes satisfy them too)
# ======================================================================
def bind_human_channel(role: Any, channel: Any) -> None:
    """Point a role's environment at the CLI's human channel."""
    role.state.env = channel


def role_session_id(role: Any) -> str:
    return role.session_id


def role_event_bus(role: Any) -> Any:
    return getattr(role, "event_bus", None)


def role_cleanup(role: Any) -> Any:
    """Return the role's async cleanup callable, or ``None``."""
    return getattr(role, "cleanup", None)


def clear_messages(role: Any) -> int:
    """Clear the role's stored message history; return the pre-clear count."""
    cm = getattr(role, "context_manager", None)
    if cm is None:
        return 0
    cleared = cm.count()
    cm.clear()
    return cleared


def runtime_name(runtime: Any) -> str:
    """The display name of a runtime's role (``?`` when unavailable)."""
    return getattr(getattr(runtime.role, "role_schema", None), "name", "?")


def runtime_role(runtime: Any) -> Any:
    return runtime.role


def fork_role(role: Any) -> Optional[Any]:
    """Fork a role's session into an independent sibling role, or ``None``."""
    fork = getattr(role, "fork_session", None)
    if fork is None:
        return None
    try:
        return fork()
    except Exception:  # noqa: BLE001 — fork is best-effort
        return None


def role_tool_count(role: Any) -> int:
    """Return the built-in tool count for a role's schema.

    The data behind the CLI's startup "flag": how many built-in tools this role
    was wired with. Read off ``role_schema`` (the declared set), so it's available
    the moment the role is built — before the executor lazily binds them. Missing
    schema / field degrades to ``0`` (a fake in tests satisfies it too).

    MCP servers are deliberately *not* counted here: they connect lazily and their
    tools surface per-turn in the ``<system-reminder>`` catalog, so they are not
    part of the one-time startup load the badge reports.
    """
    schema = getattr(role, "role_schema", None)
    tools = getattr(schema, "tools", None) or []
    return len(set(tools))


def resume_role(role: Any) -> bool:
    """Resume a role's rollout. Returns whether a rollout was found."""
    return role.resume_session()


def list_sessions(role: Any) -> list:
    """List resumable sessions for the role's session type."""
    return type(role).list_sessions()


def turn_message(text: str, image_b64s: Optional[List[str]] = None) -> Any:
    """Build the ``UserMessage`` for one turn, attaching any image payloads."""
    msg = UserMessage(content=text)
    if image_b64s:
        msg.add_metadata(IMAGES, list(image_b64s))
    return msg


def list_agent_types(cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    """List registered agent types as ``[(name, description), ...]``.

    Forward-looking: the engine's ``mote.roles.agents`` package may be empty
    today, so this can return ``[]`` — the CLI degrades gracefully. Markdown
    agents under ``.mote/agents`` (git-root walk from *cwd* + ``~/.mote/agents``)
    are registered first so they appear alongside any Python agent types.
    """
    register_md_agents(Path(cwd) if cwd else None)
    agent_registry.discover()
    out: List[Tuple[str, str]] = []
    for name, cls in agent_registry.all_agents().items():
        schema = cls.get_schema()
        out.append((name, schema.get("description", "") or ""))
    return out


__all__ = [
    "load_config",
    "build_context",
    "build_role",
    "build_control",
    "wrap_runtime",
    "bind_human_channel",
    "role_session_id",
    "role_event_bus",
    "role_cleanup",
    "clear_messages",
    "runtime_name",
    "runtime_role",
    "fork_role",
    "role_tool_count",
    "resume_role",
    "list_sessions",
    "turn_message",
    "list_agent_types",
]
