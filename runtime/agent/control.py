"""Child-agent spawn vocabulary + ambient control-plane discovery.

This LEAF module holds the *zero-cycle* vocabulary every spawn site shares —
the lifecycle taxonomy, the immutable spawn request (:class:`SpawnPlan`), the
factory's build context (:class:`SpawnContext`) — plus the discovery surface
that lets a deep call site reach the live control plane without threading it
through every signature.

It deliberately imports nothing from ``environment`` / ``roles``: the concrete
``AgentControl.spawn_agent`` + ``ChildAgentHandle`` live in ``environment``
(they reference ``AgentRuntime`` / the registry), while every *caller*
(executor tools, role capabilities) only needs the duck-typed vocabulary here.

Discovery mirrors :mod:`mote.runtime.events.context`: the Contracts-owned
control Port is held only in an **ambient contextvar** bound by the scheduler
around each turn and inherited by child asyncio tasks via context-copy.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator, TypeVar

from mote.contracts.agent import ContextPolicy, Lifecycle, RunnableAgent, SpawnContext, SpawnPlan
from mote.contracts.agent.errors import AgentLimitReached
from mote.contracts.conversation import Message
from mote.contracts.output import RunOutcome
from mote.contracts.ports.agent.control import AgentControlPort
from mote.runtime.telemetry.logging import logger

# ----------------------------------------------------------------------
# Ambient discovery (mirrors events/context.py's _ACTIVE_BUS)
# ----------------------------------------------------------------------
OutputT = TypeVar("OutputT")

_ACTIVE_CONTROL: ContextVar[AgentControlPort | None] = ContextVar("mote_agent_control", default=None)


def current_control() -> AgentControlPort | None:
    """Return the control plane bound in the current context, or ``None``."""
    return _ACTIVE_CONTROL.get()


@contextmanager
def set_control(control: AgentControlPort | None) -> Iterator[AgentControlPort | None]:
    """Bind *control* as the ambient plane for the duration of the block."""
    token = _ACTIVE_CONTROL.set(control)
    try:
        yield control
    finally:
        _ACTIVE_CONTROL.reset(token)


def resolve_control() -> AgentControlPort | None:
    """Resolve only the scheduler-bound authoritative ambient control plane."""
    return current_control()


# ----------------------------------------------------------------------
# Unified spawn → run → release helper
# ----------------------------------------------------------------------
async def spawn_and_run(
    spec: SpawnPlan[OutputT],
    message: Message | Callable[[RunnableAgent[OutputT]], Message],
    *,
    on_spawn: Callable[[RunnableAgent[OutputT]], Awaitable[None]] | None = None,
) -> RunOutcome[OutputT] | None:
    """Spawn a child through the resolved plane, run it to completion, release it.

    The one helper every ephemeral spawn site funnels through. Resolves the
    ambient plane, spawns through the single
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
    control = resolve_control()
    if control is None:
        raise RuntimeError(
            "spawn_and_run requires an active control plane; none is bound " "(ambient current_control())."
        )
    try:
        handle = await control.spawn_agent(spec)
    except AgentLimitReached as exc:
        logger.warning(f"spawn_and_run: agent limit reached for '{spec.nickname or spec.agent_role or 'agent'}': {exc}")
        return None
    async with handle:
        role = handle.agent
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
    "SpawnPlan",
    "SpawnContext",
    "spawn_and_run",
    "nullcontext",
]
