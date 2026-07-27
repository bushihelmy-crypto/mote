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

Discovery mirrors :mod:`mote.runtime.events.context`: **control walks an
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
from typing import Any, Awaitable, Callable, Iterator, Optional

from mote.contracts.errors.environment import AgentLimitReached
from mote.contracts.spawn import ContextPolicy, Lifecycle, SpawnContext, SpawnSpec
from mote.runtime.logging import logger

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
# Unified spawn → run → release helper
# ----------------------------------------------------------------------
async def spawn_and_run(
    spec: SpawnSpec,
    message: Any,
    *,
    ctx: Any = None,
    on_spawn: Optional[Callable[[Any], Awaitable[None]]] = None,
) -> Optional[Any]:
    """Spawn a child through the resolved plane, run it to completion, release it.

    The one helper every ephemeral spawn site funnels through. Resolves the
    plane (explicit ``ctx.agent_control`` → ambient), spawns through the single
    authority, runs the child inline, and tears it down — all via the handle's
    context manager so the slot is released on every exit path.

    ``message`` may be a plain message/string or a builder
    ``Callable[[role], message]`` invoked once the child role exists (so a
    caller can lower its brief through the child's own command channel).

    ``on_spawn`` is an optional async hook run on the built child role *after* it
    exists but *before* its first turn — the window a caller uses to seed
    per-agent routing state (spawn-time tier floor). It runs inside the handle's
    context manager so any teardown still fires on every exit path.

    Every child is born through the control plane — there is no plane-less
    fallback. Production always binds a plane (REPL root / scheduler turn /
    ``base_env.add_role``), so a missing plane is a wiring bug; raise rather
    than silently spawn an unmanaged child outside cap / lineage / cost.

    Returns the child's typed run result. Returns ``None`` when the spawn was
    refused by the cap or the child timed out before committing an output
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
        role = handle.runtime.role
        # Only reach into the spawned role when ``message`` is a builder — a
        # plain message/string never needs the role.
        msg = message(role) if callable(message) else message
        # Seed window: run any spawn-time hook on the built role before its
        # first turn (e.g. seed the routing tier floor from the first prompt).
        if on_spawn is not None:
            await on_spawn(role)
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
