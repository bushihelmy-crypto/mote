"""Rewrite provenance — the leaf that lets an event record its own mutations.

A control subscriber may rewrite a mutable field of a control event (tool args,
tool output). Each such change is recorded as an immutable :class:`Rewrite` on
the event itself, so a rewrite is traceable from the event alone — who changed
which field, from what to what. The provenance rides on the event
(self-describing), never in a side table that could drift from the value it
describes.

Split into its own leaf so ``event.outcome_type`` can bind each control event to
its outcome without a cycle: the outcome layer needs :class:`Rewritable` (to test
whether a rewrite target is rewritable) while the event layer imports the outcome
types — routing both through this stdlib-only leaf breaks the ``types → outcomes
→ types`` loop. Re-exported from ``common/events/types.py`` so importers can
reach it from there too.

Leaf module: imports only ``dataclasses``/``typing``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Rewrite:
    """One field mutation applied to a control event by a named subscriber.

    ``field`` is the event attribute rewritten; ``before``/``after`` are its
    values around the change; ``by`` is the rewriting subscriber's ``name``,
    stamped by the bus at the single point that pairs a subscriber with the
    event it just mutated. Immutable: a recorded rewrite is history.
    """

    field: str = ""
    before: Any = None
    after: Any = None
    by: str = ""


@dataclass
class Rewritable:
    """Mixin for a control event whose fields a subscriber may rewrite.

    Carries the ordered provenance log and the *single* generic mutation
    primitive :meth:`rewrite`, which reads the before-image and appends a
    :class:`Rewrite` in one step — so a rewrite can never be applied without
    being recorded. An event becomes rewritable by inheriting this alone; it
    hand-rolls no per-field ``rebind_*`` method, and the one recording point
    serves every rewritable event, present and future.
    """

    #: Ordered log of every rewrite applied as the event flowed through the
    #: control bucket — the audit trail an observer reads off the final event.
    rewrites: tuple[Rewrite, ...] = ()

    def rewrite(self, field: str, after: Any, *, by: str = "") -> "Rewritable":
        """Return a copy with ``field`` set to ``after`` and the change recorded.

        The before-image is read here rather than supplied, so provenance is
        captured atomically with the mutation and cannot be forged or forgotten.
        ``by`` is the rewriting subscriber's name (the bus supplies it). Any
        non-rewritten field (e.g. a tool-bound closure) is preserved by
        :func:`~dataclasses.replace`.
        """
        record = Rewrite(field=field, before=getattr(self, field), after=after, by=by)
        return replace(self, **{field: after}, rewrites=(*self.rewrites, record))


__all__ = ["Rewrite", "Rewritable"]
