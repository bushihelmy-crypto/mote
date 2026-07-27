#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The :class:`Locale` value object + BCP-47-style negotiation/fallback.

A :class:`Locale` bundles the three things a display string needs to render
correctly for a human audience: its ``code`` (identity), its ``language`` (the
base used for plural rules + catalog fallback), and its ``numbers`` symbol set.
:func:`negotiate` maps a requested tag (``zh-Hans`` / ``en_US`` / ``fr``) onto a
shipped locale, falling back down the chain to the ultimate default.

Zero dependencies beyond its i18n siblings (``plurals`` / ``numbers``); imports
nothing from ``config`` or ``cli`` — the caller passes a resolved tag in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from mote.product.i18n.numbers import DEFAULT_NUMBERS, Numbers
from mote.product.i18n.plurals import plural_category


@dataclass(frozen=True)
class Locale:
    """A resolved human display locale (identity + plural base + number symbols)."""

    code: str
    language: str
    numbers: Numbers = DEFAULT_NUMBERS

    def category(self, n: float) -> str:
        """The CLDR plural category of *n* under this locale's language."""
        return plural_category(self.language, n)


# The shipped locales. Adding a locale = a new entry here + its catalog + (if the
# language is new) its plural rule in ``plurals.py``. No call-site touches.
_LOCALES: Dict[str, Locale] = {
    "en": Locale(code="en", language="en"),
    "zh": Locale(code="zh", language="zh"),
}

#: The ultimate fallback when a requested tag matches nothing shipped.
FALLBACK_LOCALE = "en"


def available_locales() -> Tuple[str, ...]:
    """The codes of every shipped locale (registration order)."""
    return tuple(_LOCALES)


def get_locale(code: str) -> Optional[Locale]:
    """Return the shipped :class:`Locale` for *code*, or ``None``."""
    return _LOCALES.get(code)


def _base(tag: str) -> str:
    """The base language subtag of a BCP-47/POSIX tag (``zh-Hans`` → ``zh``)."""
    return tag.replace("_", "-").split("-", 1)[0].strip().lower()


def negotiate(requested: Optional[str], *, default: str = FALLBACK_LOCALE) -> Locale:
    """Map a requested locale tag onto a shipped :class:`Locale` (BCP-47-style).

    Tries the exact normalised code, then its base language, then *default*. An
    empty/None request goes straight to *default* so the result is always a real,
    shipped locale (callers never get ``None``).
    """
    if requested:
        norm = requested.replace("_", "-").strip().lower()
        if norm in _LOCALES:
            return _LOCALES[norm]
        base = _base(requested)
        if base in _LOCALES:
            return _LOCALES[base]
    return _LOCALES.get(default) or _LOCALES[FALLBACK_LOCALE]


__all__ = ["Locale", "available_locales", "get_locale", "negotiate", "FALLBACK_LOCALE"]
