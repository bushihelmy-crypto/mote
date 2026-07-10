"""EphemeralContextSource protocol — one feed of per-turn ephemeral context.

The structural slice the ``TurnContextBus`` (in ``context/turn_context``) depends
on for each pluggable feed it aggregates — git status, token-pressure notes,
background-task progress, LSP diagnostics, ... — without naming any of those
higher layers.

Mirrors ``HookRunner`` / ``FileSnapshotStore``: the bus lives in
the low ``context`` layer and must never import ``tasks`` / ``roles``, so it
takes this Protocol and the concrete sources (which DO live in those layers) are
injected by ``Role``. A source's ``render`` must be best-effort: the bus guards
every call, but a source should still prefer returning ``None`` over raising.

Leaf module: imports only ``typing``.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional, Protocol, runtime_checkable


class TurnContextPriority(IntEnum):
    """Render order of ephemeral context sources — a *named* contract.

    Sources render their blocks in ascending order of this value, and the
    ``TurnContextBus`` concatenates the survivors into one ``<system-reminder>``.
    The order is a **relevance gradient**, not a hard dependency: it decides only
    where each block sits within the reminder envelope, so a mis-ordered source
    can at most read slightly out of sequence, never break a turn. Named anyway so
    a new source picks a tier *with meaning* (a freshness warning is more urgent
    than a skill hint) instead of guessing a non-clashing integer.

    The gradient runs from *what the model should orient on first* (the tool
    catalogue, the repo state) through *live pressures and just-happened events*
    down to *ambient hints* (available skills):
    """

    TOOL_CATALOG = 5  # the available-commands catalogue — leads the reminder
    GIT = 10  # working-tree branch / status / recent commits
    TOKEN = 20  # context-budget pressure note (only when near the limit)
    COMPACTION = 25  # a just-happened compaction's summary (one-shot)
    CHANGED_FILES = 27  # files edited on disk since the agent last read them
    CODE_MAP = 28  # local structure map of the touched files (defines / imports / used-by)
    BACKGROUND_TASKS = 30  # background-task progress deltas
    DIAGNOSTICS = 40  # LSP diagnostics accumulated since last turn
    SKILL_LISTING = 45  # the catalogue of activatable skills
    SKILL_ACTIVATION = 50  # a hint that a skill matches the current prompt


#: Default render order for a source that does not declare one. Late (``SKILL_
#: ACTIVATION``) so an undeclared source trails the curated feeds rather than
#: pre-empting them.
DEFAULT_TURN_CONTEXT_PRIORITY = TurnContextPriority.SKILL_ACTIVATION


@runtime_checkable
class EphemeralContextSource(Protocol):
    """One pluggable feed of per-turn context.

    Each source renders a self-contained text block that the bus wraps (with the
    others) into a single ``<system-reminder>``. ``name`` is a stable key
    (logging / dedupe); ``priority`` is a :class:`TurnContextPriority` tier
    ordering the blocks within the envelope (lower first), read via
    ``getattr(s, "priority", DEFAULT_TURN_CONTEXT_PRIORITY)``.

    ``save_to_context`` routes the source into one of the bus's two disjoint
    buckets:

    - ``True`` (the default): the rendered block is **persisted into history**
      via ``TurnContextBus.collect_to_context`` — written once per turn through
      the ``ContextManager`` so it survives across turns and compaction.
    - ``False``: the block is **ephemeral / request-only** — gathered by
      ``TurnContextBus.collect`` and appended to the cycle's user prompt, never
      stored in history.

    A source missing the attribute is treated as ``True`` (persisted).
    """

    name: str
    priority: int
    save_to_context: bool

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        """Return this source's context block, or ``None`` when it has nothing.

        ``cwd`` is the Role's live working directory (it can move via ``cd``).
        An empty/whitespace return is treated the same as ``None`` by the bus.
        """
        ...


__all__ = [
    "EphemeralContextSource",
    "TurnContextPriority",
    "DEFAULT_TURN_CONTEXT_PRIORITY",
]
