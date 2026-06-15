"""EphemeralContextSource protocol — one feed of per-turn ephemeral context.

The structural slice the ``TurnContextBus`` (in ``context/turn_context``) depends
on for each pluggable feed it aggregates — git status, token-pressure notes,
background-task progress, LSP diagnostics, ... — without naming any of those
higher layers.

Mirrors ``LspNotifier`` / ``SessionRecorder`` / ``HookRunner``: the bus lives in
the low ``context`` layer and must never import ``tasks`` / ``roles``, so it
takes this Protocol and the concrete sources (which DO live in those layers) are
injected by ``Role``. A source's ``render`` must be best-effort: the bus guards
every call, but a source should still prefer returning ``None`` over raising.

Leaf module: imports only ``typing``.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class EphemeralContextSource(Protocol):
    """One pluggable feed of per-turn, request-only context.

    Each source renders a self-contained text block that the bus wraps (with the
    others) into a single ``<system-reminder>`` appended to the cycle's user
    prompt — never stored in history. ``name`` is a stable key (logging /
    dedupe); ``priority`` orders the blocks within the envelope (lower first).
    """

    name: str
    priority: int

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        """Return this source's context block, or ``None`` when it has nothing.

        ``cwd`` is the Role's live working directory (it can move via ``cd``).
        An empty/whitespace return is treated the same as ``None`` by the bus.
        """
        ...


__all__ = ["EphemeralContextSource"]
