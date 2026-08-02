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

* **Bootstrap/construction** (``load_config`` / ``build_role``
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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from mote.contracts.conversation import UserMessage
from mote.contracts.conversation.fields import IMAGES
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.product.agents.catalog import AgentCatalog
from mote.product.config.loader import load_config as _load_config
from mote.product.config.schema import Config
from mote.product.interaction.human_channel import PortHumanChannel
from mote.product.paths import RuntimePaths, default_runtime_paths
from mote.product.session_hosting.composition import compose_resident_agent
from mote.runtime.agent import Role
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.models.cost.report import format_total_cost
from mote.runtime.models.ratelimit import format_rate_limits
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.checkpoint import CheckpointEntry
from mote.runtime.session.checkpoint import list_checkpoints as _list_checkpoints
from mote.runtime.session.listing import SessionInfo
from mote.runtime.session.log import SessionLog

TextRole = Role[None, str]


# ======================================================================
# Bootstrap / construction
# ======================================================================
def load_config(model: str | None = None, *, paths: RuntimePaths | None = None) -> Config:
    """Load the engine config, optionally overriding the LLM model."""
    paths = paths or default_runtime_paths()
    return _load_config(
        programmatic=({"llm__model": model} if model else None),
        user_config_root=paths.user_config_root,
    )


def build_control(role: TextRole) -> tuple[AgentControl, AgentRuntime[str]]:
    """Build the control plane, adopt *role* as the root, wire it into its context.

    Returns ``(control, root_runtime)``. The plane reference is bound to the
    Role, never the shared Engine Context, so concurrent sessions cannot
    overwrite each other's spawn authority.
    """
    projection = role.wiring.dependencies.component_projection
    if projection is None:
        raise RuntimeError("resident Agent requires a component projection")
    workspace_root = projection.session_workspace_root()
    services = role.wiring.services
    if services is None or services.agent_budget is None:
        raise RuntimeError("resident Agent requires canonical budget governance")
    return compose_resident_agent(
        role,
        residency_dir=workspace_root / ".agent_residency",
        sessions_dir=workspace_root / ".agent_sessions",
        writer=role.context.disk_writer,
        governance=role.config.agents,
        budget=services.agent_budget,
        workflow_governance=services.workflow_governance,
    )


def wrap_runtime(role: TextRole) -> AgentRuntime[str]:
    """Wrap a role into a runtime for the control plane."""
    return AgentRuntime(role)


# ======================================================================
# Accessors (pure attribute operations — fakes satisfy them too)
# ======================================================================
def bind_human_channel(role: TextRole, channel: PortHumanChannel) -> None:
    """Point a role's environment at the CLI's human channel."""
    role.bind_human_interaction(channel)


def role_session_id(role: TextRole) -> str:
    return role.session_id


def role_telemetry(role: TextRole) -> TelemetryRuntime:
    return role.telemetry


def role_cleanup(role: TextRole) -> Callable[[], Awaitable[None]]:
    """Return the role's async cleanup callable, or ``None``."""
    return role.cleanup


async def clear_messages(role: TextRole) -> int:
    """Clear the role's stored message history; return the pre-clear count.

    Awaits :meth:`ContextManager.clear`, which commits a
    ``HistoryEditedEvent(reason="clear")`` before clearing both durable
    projections and the live model context. History-derived signals then
    re-derive against the empty view.
    """
    cm = role.context_manager
    cleared = cm.count()
    await cm.clear()
    return cleared


async def delete_react_units(role: TextRole, anchor_ids: Sequence[str]) -> int:
    """Delete the react-units anchored at ``anchor_ids`` on the role's history.

    Delegates to :meth:`ContextManager.delete_react_units`, which rebuilds the
    live model context without the selected turns and commits a
    ``HistoryEditedEvent`` whose IDs remove the same turns from both replayed
    projections. Returns the number of messages removed
    (``0`` when the role has no context manager or nothing matched).
    """
    cm = role.context_manager
    return await cm.delete_react_units(anchor_ids)


def list_checkpoints(role: TextRole) -> list[CheckpointEntry]:
    """List the role's whole-tree checkpoints as ``[CheckpointEntry, ...]``.

    Reads the session's rollout log; each entry is a captured user-turn snapshot
    (index, prompt preview, timestamp, commit) the user can ``/rewind`` to. An
    empty list when the feature was inert (non-repo workspace) or nothing yet
    captured.
    """
    log = SessionLog(
        role.state.session_id,
        base_dir=str(role._components.workspace_store.sessions_root),
        writer=role.context.disk_writer,
    )
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

    target: CheckpointEntry
    external: List[str]


async def rewind_files(role: TextRole, index: int) -> RewindResult | None:
    """Roll the working tree back to checkpoint ``index``; return the outcome.

    Auto-captures the *current* tree state first (a checkpoint labelled "before
    rewind") so the rewind itself is reversible, then restores the target
    checkpoint's commit. Before restoring, diffs the target turn's after-image
    (the tree the agent left) against that just-captured current tree to detect
    files an external process changed since the agent finished — reported as
    :attr:`RewindResult.external`. Returns a :class:`RewindResult` on success, or
    ``None`` when the index is out of range or the restore failed.
    """
    log = SessionLog(
        role.state.session_id,
        base_dir=str(role._components.workspace_store.sessions_root),
        writer=role.context.disk_writer,
    )
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


def runtime_name(runtime: AgentRuntime[str]) -> str:
    """The display name of a runtime's role (``?`` when unavailable)."""
    return runtime.role.role_schema.name


def runtime_role(runtime: AgentRuntime[str]) -> TextRole:
    return runtime.role


async def fork_role(role: TextRole) -> TextRole | None:
    """Fork a role's session into an independent sibling role, or ``None``."""
    try:
        return await role.fork_session()
    except Exception:  # noqa: BLE001 — fork is best-effort
        return None


def role_tool_count(role: TextRole) -> int:
    """Return the built-in tool count for a role's schema.

    The data behind the CLI's startup "flag": how many built-in tools this role
    was wired with. Read off ``role_schema`` (the declared set), so it's available
    the moment the role is built — before the executor lazily binds them. Missing
    schema / field degrades to ``0`` (a fake in tests satisfies it too).

    MCP servers are deliberately *not* counted here: they connect lazily and their
    tools surface per-turn in the ``<system-reminder>`` catalog, so they are not
    part of the one-time startup load the badge reports.
    """
    tools = role.role_schema.tools
    return len(set(tools))


def role_deferred_tool_count(role: TextRole) -> int:
    """How many of the loaded tools are *deferred* (hidden until searched).

    A deferred tool is bound and dispatchable but its schema is withheld from the
    model until discovered via ``SearchTools`` — so it counts toward the loaded
    total, and the badge annotates how many of that total start deferred.

    Respects the global tool-search master switch: when
    ``config.tools.tool_search.enabled`` is off no tool is deferred (every one is
    fully visible), so this reports ``0``. Missing schema / config degrades to
    ``0`` (a fake in tests without a ``deferred_tools`` field satisfies it too).
    """
    if not role.config.tools.tool_search.enabled:
        return 0
    deferred = role.role_schema.deferred_tools
    return len(set(deferred))


def usage_report(role: TextRole) -> str:
    """The ``/usage`` block: session cost + provider rate-limit quota.

    Reads the two rolling trackers off the role's shared router
    :class:`~mote.runtime.models.clients.context.Context` — ``cost_manager`` (accumulated
    spend) and ``rate_limit_tracker`` (latest observed provider quota) — and
    renders each via its own report module. A role without a resolvable context
    (a bare fake in tests) degrades to a plain "unavailable" line rather than
    raising, keeping the command host-surface total.
    """
    context = role.context
    cost_block = format_total_cost(context.cost_manager)
    limit_block = format_rate_limits(context.rate_limit_tracker)
    return f"{cost_block}\n\n{limit_block}"


def resume_role(role: TextRole) -> bool:
    """Resume a role's rollout. Returns whether a rollout was found."""
    return role.resume_session()


def list_sessions(role: TextRole) -> list[SessionInfo]:
    """List resumable sessions for the role's session type."""
    projection = role.wiring.dependencies.component_projection
    if projection is None:
        raise RuntimeError("resident Agent requires a component projection")
    workspace_root = projection.session_workspace_root()
    if workspace_root is None:
        raise ValueError("Agent composition requires a session workspace root")
    return type(role).list_sessions(base_dir=str(workspace_root / ".agent_sessions"))


def turn_message(text: str, image_b64s: list[str] | None = None) -> UserMessage:
    """Build the ``UserMessage`` for one turn, attaching any image payloads."""
    msg = UserMessage(content=text)
    if image_b64s:
        msg.metadata[IMAGES] = list(image_b64s)
    return msg


def list_agent_types(agent_catalog: AgentCatalog[str]) -> list[tuple[str, str]]:
    """List snapshotted agent types as ``[(name, description), ...]``.

    The product Agent declaration package may be empty
    today, so this can return ``[]`` — the CLI degrades gracefully. Markdown
    agents under ``.mote/agents`` appear alongside Python definitions because
    both were composed into this exact Application snapshot.
    """
    out: list[tuple[str, str]] = []
    for name, definition in agent_catalog.all_agents().items():
        out.append((name, definition.description))
    return out


__all__ = [
    "load_config",
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
