"""Deterministic text elision facts and model-facing markers.

A zero-dependency value object + pure decision functions. This is the one place
that owns the *shape* of the inline "omitted" marker the model reads; every
truncation site across the codebase renders through :meth:`Elision.render_for_model`
so the wording stays uniform while each domain keeps its own noun / extra detail.

Layering: this module lives in the bottom ``common`` layer and imports only the
stdlib — no I/O, no provider shapes, no presentation concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class ElisionUnit(Enum):
    """The unit an elision is measured in."""

    CHARS = "chars"
    BYTES = "bytes"
    LINES = "lines"
    TOKENS = "tokens"

    def label(self) -> str:
        return self.value


class ElisionStrategy(Enum):
    """Which region of the original text was kept."""

    HEAD = "head"
    TAIL = "tail"
    HEAD_TAIL = "head_tail"
    MIDDLE = "middle"


@dataclass(frozen=True, slots=True)
class Elision:
    """An immutable record of one truncation: what was dropped and how.

    Holds only facts (unit / omitted / total / strategy) plus a single method to
    render the canonical inline marker. The caller owns any surrounding newlines
    or indentation.
    """

    unit: ElisionUnit
    omitted: int  # number of units dropped
    total: int  # original size (in ``unit``)
    strategy: ElisionStrategy

    def render_for_model(
        self,
        *,
        noun: str | None = None,
        format_count: Callable[[int], str] = str,
        extra: str | None = None,
        with_total: bool = False,
    ) -> str:
        """Render the canonical inner marker ``[... N <word> omitted ...]``.

        - ``noun`` overrides the unit label (e.g. ``"more changed lines"``).
        - ``format_count`` formats both the omitted count and the total (e.g.
          ``format_file_size``); defaults to :func:`str`.
        - ``extra`` appends a trailing detail before the closing ``...``.
        - ``with_total`` inserts `` of <total> total`` after the noun.

        The returned string carries no surrounding whitespace — the caller wraps
        it with whatever newlines/indent the site needs.
        """
        word = noun if noun is not None else self.unit.label()
        mid = f" {word}" if word else ""  # empty noun (size-formatted count) → no filler word
        of = f" of {format_count(self.total)} total" if with_total else ""
        tail = f" {extra}" if extra else ""
        return f"[... {format_count(self.omitted)}{mid} omitted{of}{tail} ...]"


def cap_head_tail(text: str, limit: int, *, unit: ElisionUnit = ElisionUnit.CHARS) -> tuple[str, Elision | None]:
    """Keep ``limit`` head + tail characters, drop the middle with a marker.

    Returns the (possibly rewritten) text and an :class:`Elision` describing the
    drop, or ``(text, None)`` when under limit / guarded. The marker is wrapped
    in ``\\n...\\n`` so this packs the full head+tail rendering that callers can
    use verbatim.
    """
    if limit <= 0 or len(text) <= limit:
        return text, None
    head = limit // 2
    tail = limit - head
    el = Elision(unit, len(text) - limit, len(text), ElisionStrategy.HEAD_TAIL)
    return f"{text[:head]}\n{el.render_for_model()}\n{text[-tail:]}", el


def cap_head(text: str, limit: int, *, unit: ElisionUnit = ElisionUnit.CHARS) -> tuple[str, Elision | None]:
    """Keep the leading ``limit`` characters, dropping the tail.

    Returns the raw head slice + an :class:`Elision`, or ``(text, None)`` when
    under limit / guarded. Unlike :func:`cap_head_tail` this does NOT embed a
    marker — the caller renders its own (cut boundaries / count formats vary).
    """
    if limit <= 0 or len(text) <= limit:
        return text, None
    el = Elision(unit, len(text) - limit, len(text), ElisionStrategy.HEAD)
    return text[:limit], el
