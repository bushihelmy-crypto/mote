#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``backend`` — the single binding seam onto the mote engine (Ports & Adapters).

This is the **only** module in ``mote.product.cli`` that imports mote's concrete
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

Also exposes the Application's agent catalog snapshot (``list_agent_types`` +
``build_role(agent_type=...)``) so the CLI can spawn typed agents without
building a parallel type system.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from mote.contracts.constants.messages import IMAGES
from mote.contracts.schema import UserMessage
from mote.contracts.settings.watching import FileWatchConfig
from mote.orchestration.environment.control import AgentControl
from mote.orchestration.environment.runtime import AgentRuntime
from mote.orchestration.environment.store import ResidencyStore
from mote.product.agents import CodingAgentFactory
from mote.product.integrations.bootstrap import (
    builtin_model_gateway,
    builtin_provider_registry,
    builtin_service_gateway,
)
from mote.runtime.agent import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.config.loader import load_config as _load_config
from mote.runtime.disk.async_io import run_disk_io
from mote.runtime.models.clients.context import Context
from mote.runtime.models.cost.report import format_total_cost
from mote.runtime.models.failover import (
    LocalModelCallJournal,
    LocalModelOperatorAuditStore,
    ResourceAdmissionController,
    default_model_call_journal_root,
)
from mote.runtime.models.failover.operator import default_model_operator_audit_path
from mote.runtime.models.ratelimit import format_rate_limits
from mote.runtime.service_gateway import LocalServiceCallJournal, default_service_call_journal_root
from mote.runtime.session.checkpoint import list_checkpoints as _list_checkpoints
from mote.runtime.session.log import SessionLog
from mote.runtime.tools.agent_registry import AgentCatalog
from mote.runtime.tools.mcp.config_source import MCP_CONFIG_FILE_NAME, load_mcp_servers
from mote.runtime.tools.permission.settings_source import load_permission_rules
from mote.runtime.vcs import find_git_root


# ======================================================================
# Bootstrap / construction
# ======================================================================
def load_config(model: Optional[str] = None) -> Any:
    """Load the engine config, optionally overriding the LLM model."""
    return _load_config(programmatic=({"llm__model": model} if model else None))


def build_context(
    config: Any,
    *,
    providers: Any = None,
    media_providers: Any = None,
    search_backends: Any = None,
) -> Any:
    """Build the engine :class:`Context` (opaque handle to app.py)."""
    providers = providers or builtin_provider_registry()
    context = Context(config=config, provider_factory=providers.create)
    model_operator = ResourceAdmissionController(
        breaker_config=config.resilience.to_breaker_config(),
        operator_audit=LocalModelOperatorAuditStore(default_model_operator_audit_path()),
    )
    context.model_operator = model_operator
    context.model_gateway = builtin_model_gateway(
        config.models,
        providers=providers,
        cost_tracker=context.cost_manager,
        admission_controller=model_operator,
        model_call_journal=LocalModelCallJournal(default_model_call_journal_root()),
    )
    context.service_gateway = builtin_service_gateway(
        config.multimodal,
        config.tools.web_search,
        model_gateway=context.model_gateway,
        media_providers=media_providers,
        search_backends=search_backends,
        admission_controller=model_operator,
        service_call_journal=LocalServiceCallJournal(default_service_call_journal_root()),
    )
    return context


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


def _cli_file_watch_roots(cwd: Optional[str]) -> list[str]:
    """Anchor CLI hot reload to a config file, not the whole Git worktree."""
    base = Path(cwd) if cwd else Path.cwd()
    return [str(base / ".mote" / MCP_CONFIG_FILE_NAME)]


def build_role(
    *,
    services: Any,
    agent_factory: CodingAgentFactory,
    agent_catalog: AgentCatalog,
    name: str,
    tools: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[Any]:
    """Unified role factory (initial / new / resume / typed spawn).

    ``agent_type`` empty → the generic ``Role`` path (explicit schema + state).
    ``agent_type`` given → look it up in the Application's Agent catalog; an unknown
    type returns ``None`` (the caller surfaces the failure), otherwise the agent
    class self-configures its own schema/tools.

    The generic path is the interactive top-level role, so it opts into the two
    "watch the workspace" conveniences a human at a REPL expects: every MCP server
    in ``mcp_config.json`` is loaded (its tools surface in the per-turn catalog),
    and a file watcher hot-reloads MCP servers *and* skills when their config
    files change mid-session. **Tools**, by contrast, follow RoleSchema's curated
    default when no explicit ``tools`` are passed — so what the CLI reports (and
    what the agent is actually wired with) is exactly the declared set, not the
    full registered toolbox. Completion and messaging control verbs such as
    End/Reply/Ask remain outside the curated surface; persistent Runtime tools
    expose their own user-control handoff action. Typed agents self-configure
    and are left untouched.

    Markdown and Python Agent definitions were frozen into ``agent_catalog`` by
    Product assembly, so typed spawn and the Agent tool observe the same version.
    """
    if agent_type:
        cls = agent_catalog.get(agent_type)
        if cls is None:
            return None
        role = agent_factory.build(
            cls,
            services=services,
            name=name,
        )
    else:
        schema_kwargs: dict = {
            "name": name,
            "mcps": _discover_mcps(cwd),
            "file_watch": FileWatchConfig(
                enabled=True,
                roots=_cli_file_watch_roots(cwd),
                reload_mcp=True,
                reload_skills=True,
            ),
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
        role = agent_factory.build(
            Role,
            name=name,
            role_schema=schema,
            state=state,
            services=services,
        )
    _apply_cwd(role, cwd)
    return role


def build_control(role: Any) -> Tuple[Any, Any]:
    """Build the control plane, adopt *role* as the root, wire it into its context.

    Returns ``(control, root_runtime)``. The plane reference is bound to the
    Role, never the shared Engine Context, so concurrent sessions cannot
    overwrite each other's spawn authority.
    """
    control = AgentControl(
        session_id=role.session_id,
        store=ResidencyStore(writer=role.context.disk_writer),
    )
    runtime = wrap_runtime(role)
    control.add_agent(runtime, root=True)
    role.agent_control = control
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


def role_telemetry(role: Any) -> Any:
    return getattr(role, "telemetry", None)


def role_cleanup(role: Any) -> Any:
    """Return the role's async cleanup callable, or ``None``."""
    return getattr(role, "cleanup", None)


async def clear_messages(role: Any) -> int:
    """Clear the role's stored message history; return the pre-clear count.

    Awaits :meth:`ContextManager.clear`, which commits a
    ``HistoryEditedEvent(reason="clear")`` before clearing both durable
    projections and the live model context. History-derived signals then
    re-derive against the empty view.
    """
    cm = getattr(role, "context_manager", None)
    if cm is None:
        return 0
    cleared = cm.count()
    await cm.clear()
    return cleared


async def delete_react_units(role: Any, anchor_ids) -> int:
    """Delete the react-units anchored at ``anchor_ids`` on the role's history.

    Delegates to :meth:`ContextManager.delete_react_units`, which rebuilds the
    live model context without the selected turns and commits a
    ``HistoryEditedEvent`` whose IDs remove the same turns from both replayed
    projections. Returns the number of messages removed
    (``0`` when the role has no context manager or nothing matched).
    """
    cm = getattr(role, "context_manager", None)
    if cm is None:
        return 0
    return await cm.delete_react_units(anchor_ids)


def list_checkpoints(role: Any) -> list:
    """List the role's whole-tree checkpoints as ``[CheckpointEntry, ...]``.

    Reads the session's rollout log; each entry is a captured user-turn snapshot
    (index, prompt preview, timestamp, commit) the user can ``/rewind`` to. An
    empty list when the feature was inert (non-repo workspace) or nothing yet
    captured.
    """
    log = SessionLog(role.state.session_id, writer=role.context.disk_writer)
    return _list_checkpoints(log)


@dataclass
class RewindResult:
    """The outcome of a ``/rewind`` — the target plus any external-edit warning.

    ``target`` is the :class:`CheckpointEntry` the tree was rolled back to.
    ``external`` lists paths a process *other* than the agent changed since that
    turn ended (the diff of the turn's after-image against the tree captured just
    before rewinding). Non-empty means the rewind overwrote edits the agent did
    not make — surfaced so the user knows what was clobbered; the rewind still
    proceeds (and is itself reversible via the auto-saved "before rewind" point).
    """

    target: Any
    external: List[str]


async def rewind_files(role: Any, index: int) -> Optional[RewindResult]:
    """Roll the working tree back to checkpoint ``index``; return the outcome.

    Auto-captures the *current* tree state first (a checkpoint labelled "before
    rewind") so the rewind itself is reversible, then restores the target
    checkpoint's commit. Before restoring, diffs the target turn's after-image
    (the tree the agent left) against that just-captured current tree to detect
    files an external process changed since the agent finished — reported as
    :attr:`RewindResult.external`. Returns a :class:`RewindResult` on success, or
    ``None`` when the index is out of range or the restore failed.
    """
    log = SessionLog(role.state.session_id, writer=role.context.disk_writer)
    entries = await run_disk_io(_list_checkpoints, log)
    if not (0 <= index < len(entries)):
        return None
    target = entries[index]
    work_dir = target.working_dir or role.state.project_root or role.state.working_dir
    if not work_dir:
        return None
    parent = entries[-1].commit if entries else None
    result = await run_disk_io(
        role.file_operations.rewind,
        working_dir=work_dir,
        target_commit=target.commit,
        parent_commit=parent,
        prompt_index=len(entries),
        after_commit=target.after_commit,
    )
    return RewindResult(target=target, external=list(result.external_paths))


def runtime_name(runtime: Any) -> str:
    """The display name of a runtime's role (``?`` when unavailable)."""
    return getattr(getattr(runtime.role, "role_schema", None), "name", "?")


def runtime_role(runtime: Any) -> Any:
    return runtime.role


async def fork_role(role: Any) -> Optional[Any]:
    """Fork a role's session into an independent sibling role, or ``None``."""
    fork = getattr(role, "fork_session", None)
    if fork is None:
        return None
    try:
        return await fork()
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


def role_deferred_tool_count(role: Any) -> int:
    """How many of the loaded tools are *deferred* (hidden until searched).

    A deferred tool is bound and dispatchable but its schema is withheld from the
    model until discovered via ``SearchTools`` — so it counts toward the loaded
    total, and the badge annotates how many of that total start deferred.

    Respects the global tool-search master switch: when
    ``config.tools.tool_search.enabled`` is off no tool is deferred (every one is
    fully visible), so this reports ``0``. Missing schema / config degrades to
    ``0`` (a fake in tests without a ``deferred_tools`` field satisfies it too).
    """
    config = getattr(role, "config", None)
    tools_cfg = getattr(config, "tools", None)
    search_cfg = getattr(tools_cfg, "tool_search", None)
    if search_cfg is not None and not getattr(search_cfg, "enabled", True):
        return 0
    schema = getattr(role, "role_schema", None)
    deferred = getattr(schema, "deferred_tools", None) or []
    return len(set(deferred))


def usage_report(role: Any) -> str:
    """The ``/usage`` block: session cost + provider rate-limit quota.

    Reads the two rolling trackers off the role's shared router
    :class:`~mote.runtime.models.clients.context.Context` — ``cost_manager`` (accumulated
    spend) and ``rate_limit_tracker`` (latest observed provider quota) — and
    renders each via its own report module. A role without a resolvable context
    (a bare fake in tests) degrades to a plain "unavailable" line rather than
    raising, keeping the command host-surface total.
    """
    try:
        context = role.context
    except Exception:  # noqa: BLE001 — a role without a bound context degrades cleanly
        return "Usage unavailable (no active context)."
    cost_block = format_total_cost(context.cost_manager)
    limit_block = format_rate_limits(context.rate_limit_tracker)
    return f"{cost_block}\n\n{limit_block}"


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


def list_agent_types(agent_catalog: AgentCatalog) -> List[Tuple[str, str]]:
    """List snapshotted agent types as ``[(name, description), ...]``.

    Forward-looking: the engine's ``mote.runtime.agent.agents`` package may be empty
    today, so this can return ``[]`` — the CLI degrades gracefully. Markdown
    agents under ``.mote/agents`` appear alongside Python definitions because
    both were composed into this exact Application snapshot.
    """
    out: List[Tuple[str, str]] = []
    for name, cls in agent_catalog.all_agents().items():
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
    "role_telemetry",
    "role_cleanup",
    "clear_messages",
    "delete_react_units",
    "list_checkpoints",
    "rewind_files",
    "RewindResult",
    "runtime_name",
    "runtime_role",
    "fork_role",
    "role_tool_count",
    "role_deferred_tool_count",
    "resume_role",
    "list_sessions",
    "turn_message",
    "list_agent_types",
]
