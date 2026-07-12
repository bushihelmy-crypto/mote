"""Event outcome — reuse the hook fold machinery as the bus outcome type.

The event spine and the hook layer share the same "fold N influences into one"
semantics (deny > ask > allow, accumulated additional_context, sticky stop).
Rather than duplicate it, the bus re-exports ``common/hook/types.py``'s
:class:`HookOutcome` + :func:`fold` + ``EMPTY`` under event-flavored names. Both
live in ``common`` (the bottom layer), so there is no new dependency.
"""

from __future__ import annotations

from metagpt.common.hook.types import EMPTY, HookOutcome, fold

#: The folded influence a control event's subscribers have on the host.
EventOutcome = HookOutcome

__all__ = ["EventOutcome", "HookOutcome", "fold", "EMPTY"]
