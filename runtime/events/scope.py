"""Activity scope — the ambient execution-lineage spine.

A :class:`contextvars.ContextVar` holding the current *execution lineage*: the
stack of nested orchestrators (a ``run_graph`` graph → one of its nodes → a tool
that node dispatches; or in future a sub-agent, a background task) that the code
running right now sits inside. Deep call sites — most importantly the
:class:`~mote.runtime.tools.tool_executor.ToolExecutor` at its emit sites — *pull* the
current scope at emit time and stamp it onto the event they fan out, exactly the
way they already pull the ambient telemetry runtime from
``runtime/events/context.py`` rather than threading it through every signature.

This keeps the governed chokepoints frozen: ``dispatch_tool`` / ``run_command``
signatures never gain a ``scope`` argument, so the permission/hook/ledger seam is
untouched. Lineage is carried out-of-band on this contextvar and only *observed*
at the two executor emit sites.

Leaf module by construction: it imports only ``contextlib``/``contextvars``/
``typing``, so both ``executor.*`` (producers, which stamp scope) and ``cli.*``
(consumers, which read it back off events) depend *down* into it — never
sideways. Empty scope (``()``) is the top level, so every existing emit that
never pushes a scope is byte-for-byte unchanged.

Contextvars copy their reference into ``asyncio.create_task``-spawned coroutines
(the same mechanism ``_failure_sink`` / ``_progress_ctx`` rely on in
``bggraph/from_spec.py``), so a ``map`` node's ``gather`` children and a ``fold``
node's serial loop inherit an ambient scope for free — no per-child threading.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mote.contracts.events.scope import ScopePath, ScopeRef

_scope: ContextVar[ScopePath] = ContextVar("mote_activity_scope", default=())


def current_scope() -> ScopePath:
    """Return the lineage bound in the current context, or ``()`` at top level."""
    return _scope.get()


@contextmanager
def push_scope(ref: ScopeRef) -> Iterator[ScopePath]:
    """Push ``ref`` onto the lineage for the duration of the ``with`` block.

    Mirrors ``bind_telemetry`` / ``set_progress_sink``: sets the extended path, yields
    it, and always ``reset``\\s the token in ``finally`` so an exception unwinds
    the scope cleanly (a raising node body pops its scope, never leaks it to a
    sibling).
    """
    extended = _scope.get() + (ref,)
    token = _scope.set(extended)
    try:
        yield extended
    finally:
        _scope.reset(token)


__all__ = ["ScopeRef", "ScopePath", "current_scope", "push_scope"]
