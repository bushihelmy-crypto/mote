"""Shared child-agent plumbing for the code-review pipeline.

The pipeline spawns short-lived child :class:`Role` instances in three places —
the ``plan`` gate, the per-file ``review_unit`` leaf, and the ``review_filter``
self-critique — all with the same shape: a read-only, bypass-permission Role
that runs one prompt and whose terminal summary (``state.last_end_output``) is
read back, then cleaned up. This module factors that shape into one place so the
three callers stay thin and consistent.

The JSON-array extraction (agents end their turn with a JSON array) lives here
too, since all three callers parse the same way.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, List, Optional

from metagpt.common.agent_control import Lifecycle, SpawnContext, SpawnSpec, spawn_and_run
from metagpt.common.logs import logger
from metagpt.common.schema import UserMessage
from metagpt.common.schema.permission_config import PermissionConfig
from metagpt.roles.role import Role
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState


def build_child_role(
    *,
    name: str,
    system_prompt: str,
    repo_dir: str = "",
    parent_session_id: str = "",
    tools: Optional[List[str]] = None,
):
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
    # Imported lazily so this module can be scanned for tool registration
    # without pulling the full roles stack at import time.

    schema = RoleSchema(
        name=name,
        profile=name,
        goal="Assist the code-review pipeline.",
        command_protocol="native",
        tools=["Read", "Grep", "Glob"] if tools is None else list(tools),
        permissions=PermissionConfig(mode="bypass"),
        system_prompt=system_prompt,
        use_summary=False,
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


async def run_child(
    factory: Callable[[SpawnContext], Any],
    prompt: str,
    *,
    label: str = "child",
    ctx: Any = None,
) -> Optional[str]:
    """Spawn a child via *factory*, run it on *prompt*, return its summary text.

    Routes through the single spawn authority (``spawn_and_run`` resolves the
    ambient control plane), so every code-review leaf is born on the plane:
    cap / depth / lineage all apply, and a refused spawn (cap reached) degrades
    to ``None`` exactly like a run failure. A plane is always bound in
    production; ``spawn_and_run`` raises if none is, and this helper's
    best-effort ``except`` turns that (like any other failure) into ``None``.

    Best-effort: any failure is logged and yields ``None``. The child is always
    cleaned up (by the spawn helper / handle).

    Args:
        factory: Builds an unstarted child Role from a :class:`SpawnContext`.
        prompt: The user message driving the run.
        label: Short tag (used as the child nickname + log lines on failure).
        ctx: Optional carrier of an explicit ``agent_control`` plane.

    Returns:
        The child's terminal summary (possibly empty string), or ``None`` if the
        spawn was refused or the run raised.
    """
    spec = SpawnSpec(
        role_factory=factory,
        nickname=label,
        agent_role=label,
        lifecycle=Lifecycle.EPHEMERAL,
    )
    try:
        return await spawn_and_run(spec, UserMessage(content=prompt), ctx=ctx)
    except Exception as e:  # noqa: BLE001 — leaf isolation
        logger.warning(f"code_review: {label} run failed: {e}")
        return None


async def run_child_for_text(role, prompt: str, *, label: str = "child") -> Optional[str]:
    """Run a *prebuilt* child *role* on *prompt* and return its summary text.

    Thin shim over :func:`run_child` for the three pipeline callers that build
    the role first (via :func:`build_child_role`) and then run it. The factory
    simply returns the already-constructed role, so the spawn plane still owns
    its cap slot, lineage, and teardown. Kept as a named function so the
    pipeline's existing monkeypatch seams stay intact.

    Returns:
        ``role.state.last_end_output`` (possibly empty string), or ``None`` if
        the spawn was refused or the run raised.
    """
    return await run_child(lambda _spawn_ctx: role, prompt, label=label)


def extract_json_array(text: str) -> Optional[list]:
    """Best-effort extraction of a JSON array from an agent's final output.

    Tries, in order: a fenced ```json block, a fenced ``` block, the whole
    text, then the first bare ``[ ... ]`` span. Returns the parsed list or
    ``None`` when nothing parses to a list.
    """
    if not text:
        return None
    text = text.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: List[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)

    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


__all__ = [
    "build_child_role",
    "run_child",
    "run_child_for_text",
    "extract_json_array",
]
