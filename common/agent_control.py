"""Child-agent spawn vocabulary + ambient control-plane discovery.

This LEAF module holds the *zero-cycle* vocabulary every spawn site shares —
the lifecycle taxonomy, the immutable spawn request (:class:`SpawnSpec`), the
factory's build context (:class:`SpawnContext`) — plus the discovery surface
that lets a deep call site reach the live control plane without threading it
through every signature.

It deliberately imports nothing from ``environment`` / ``roles``: the concrete
``AgentControl.spawn_agent`` + ``ChildAgentHandle`` live in ``environment``
(they reference ``AgentRuntime`` / the registry), while every *caller*
(executor tools, role capabilities) only needs the duck-typed vocabulary here.

Discovery mirrors :mod:`mote.common.events.context`: **control walks an
explicit reference** (``ctx.agent_control``) first, then falls back to an
**ambient contextvar** (:func:`current_control`) bound by the scheduler around
each turn and inherited by child asyncio tasks via context-copy. A lost
contextvar can only ever degrade a spawn to a local, plane-less construction —
it never breaks the cap/lineage invariants, because the single authority
(``spawn_agent``) is still the only place those are enforced.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Optional

from mote.common.exception import AgentLimitReached
from mote.common.logs import logger

# ----------------------------------------------------------------------
# Ambient discovery (mirrors events/context.py's _ACTIVE_BUS)
# ----------------------------------------------------------------------
_ACTIVE_CONTROL: ContextVar[Optional[Any]] = ContextVar("mote_agent_control", default=None)


def current_control() -> Optional[Any]:
    """Return the control plane bound in the current context, or ``None``."""
    return _ACTIVE_CONTROL.get()


@contextmanager
def set_control(control: Optional[Any]) -> Iterator[Optional[Any]]:
    """Bind *control* as the ambient plane for the duration of the block."""
    token = _ACTIVE_CONTROL.set(control)
    try:
        yield control
    finally:
        _ACTIVE_CONTROL.reset(token)


def resolve_control(ctx: Any = None) -> Optional[Any]:
    """Resolve the authoritative control plane (explicit ref → ambient → None).

    Prefers an explicit ``ctx.agent_control`` (a :class:`Context` carrying the
    plane), then the ambient :func:`current_control` bound by the scheduler. The
    explicit reference always wins so a caller that knows its plane is never
    overridden by a stale ambient one.
    """
    explicit = getattr(ctx, "agent_control", None) if ctx is not None else None
    if explicit is not None:
        return explicit
    return current_control()


# ----------------------------------------------------------------------
# Lifecycle taxonomy
# ----------------------------------------------------------------------
class Lifecycle(Enum):
    """How a spawned child is driven.

    ``MANAGED`` children are added to the scheduler and live until they reach a
    final status (the caller awaits via the handle's ``join``). ``EPHEMERAL``
    children are run inline by the caller to completion (one turn, summary read
    back), never entering the scheduler.
    """

    MANAGED = "managed"
    EPHEMERAL = "ephemeral"


class ContextPolicy(Enum):
    """How the spawn authority provisions a child's LLM :class:`Context`.

    Context provisioning is owned by the single authority (``spawn_agent``), not
    the factory: a factory declares *what agent* (schema + state) and never
    touches context. This policy declares *where the child's context comes from*,
    which the authority enforces unconditionally.

    ``FRESH`` gives the child an independent Context built from the spawn's
    config — its own :class:`CostTracker` becomes a distinct node under the
    parent in the cost tree. ``SHARE_PARENT`` hands the child the parent's own
    Context (fork-like spawns), so the shared tracker is deduped in the cost tree.
    """

    FRESH = "fresh"
    SHARE_PARENT = "share_parent"


# ----------------------------------------------------------------------
# Spawn request + factory context (pure data; all duck-typed)
# ----------------------------------------------------------------------
@dataclass
class SpawnContext:
    """The build context handed to a :class:`SpawnSpec`'s ``role_factory``.

    Carries everything the factory needs to construct a child that participates
    in the tree: its reserved ``agent_path``, the parent linkage, and the
    parent's config / cost tracker (so the child can share them).
    """

    parent_id: Optional[str] = None
    agent_path: Optional[Any] = None  # AgentPath (duck-typed to avoid the import)
    cwd: Optional[str] = None
    config: Optional[Any] = None
    # Optional: the parent's CostTracker. The cost mirror tree no longer relies
    # on this (it adopts the child's own tracker as its node bucket); kept so a
    # factory that shares the parent's tracker (skill_fork) still can.
    parent_cost_tracker: Optional[Any] = None
    parent_session_id: str = ""


@dataclass
class SpawnSpec:
    """An immutable request to spawn one child agent through ``spawn_agent``.

    ``role_factory`` receives a :class:`SpawnContext` and returns an unstarted,
    duck-typed Role. Everything else parameterizes how the single spawn
    authority registers + drives the child.
    """

    role_factory: Callable[[SpawnContext], Any]
    nickname: Optional[str] = None
    parent_id: Optional[str] = None
    lifecycle: Lifecycle = Lifecycle.EPHEMERAL
    cost_rollup: bool = True
    watch_completion: bool = True
    max_depth: Optional[int] = None
    agent_role: str = ""
    # Where the child's LLM Context comes from. The single authority
    # (``spawn_agent``) provisions it per this policy; the factory never touches
    # context. FRESH = independent Context (own cost node); SHARE_PARENT = the
    # parent's own Context (shared, deduped in the cost tree — fork-like spawns).
    context_policy: ContextPolicy = ContextPolicy.FRESH


# ----------------------------------------------------------------------
# Unified spawn → run → release helper
# ----------------------------------------------------------------------
async def spawn_and_run(spec: SpawnSpec, message: Any, *, ctx: Any = None) -> Optional[str]:
    """Spawn a child through the resolved plane, run it to completion, release it.

    The one helper every ephemeral spawn site funnels through. Resolves the
    plane (explicit ``ctx.agent_control`` → ambient), spawns through the single
    authority, runs the child inline, and tears it down — all via the handle's
    context manager so the slot is released on every exit path.

    ``message`` may be a plain message/string or a builder
    ``Callable[[role], message]`` invoked once the child role exists (so a
    caller can lower its brief through the child's own command channel).

    Every child is born through the control plane — there is no plane-less
    fallback. Production always binds a plane (REPL root / scheduler turn /
    ``base_env.add_role``), so a missing plane is a wiring bug; raise rather
    than silently spawn an unmanaged child outside cap / lineage / cost.

    Returns the child's terminal summary (possibly an empty string). Returns
    ``None`` only when the spawn was refused by the cap
    (:class:`AgentLimitReached`); other run failures propagate to the caller,
    which decides how to degrade.
    """
    control = resolve_control(ctx)
    if control is None:
        raise RuntimeError(
            "spawn_and_run requires an active control plane; none is bound "
            "(explicit ctx.agent_control or ambient current_control())."
        )
    try:
        handle = await control.spawn_agent(spec)
    except AgentLimitReached as exc:
        logger.warning(f"spawn_and_run: agent limit reached for '{spec.nickname or spec.agent_role or 'agent'}': {exc}")
        return None
    async with handle:
        # Only reach into the spawned role when ``message`` is a builder — a
        # plain message/string never needs the role, so we don't touch the
        # handle's runtime in that (common) case.
        msg = message(handle.runtime.role) if callable(message) else message
        return await handle.run_to_completion(msg)


__all__ = [
    "current_control",
    "set_control",
    "resolve_control",
    "Lifecycle",
    "ContextPolicy",
    "SpawnSpec",
    "SpawnContext",
    "spawn_and_run",
    "nullcontext",
]
