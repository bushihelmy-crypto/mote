#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLDR plural-category seam for the human display layer.

``plural_category(language, n)`` returns one of the CLDR categories
(``zero one two few many other``) so a locale's messages can pick the right
grammatical form for a count. Only the *shipped* languages carry a rule — a new
locale is a new rule function registered here (data, not new call-site code),
never a change to the callers. Bounded YAGNI: we do not pre-ship rules for
languages we don't yet translate.

Zero dependencies. This is the human-locale sibling of ``common/text/plural.py``
(which stays deliberately English-only for *model*-facing text); the two never
mix so the "model text = English" invariant is preserved.
"""
from __future__ import annotations

from typing import Callable, Dict

# CLDR category identifiers (the full closed set; a language uses a subset).
ZERO = "zero"
ONE = "one"
TWO = "two"
FEW = "few"
MANY = "many"
OTHER = "other"


def _english(n: float) -> str:
    """English cardinal rule: ``1`` is ``one``; everything else is ``other``."""
    return ONE if n == 1 else OTHER


def _chinese(n: float) -> str:
    """Chinese has no count inflection — every cardinal is ``other`` (CLDR)."""
    return OTHER


# Registry keyed by base language. Adding fr/ru/ar = registering their rule here.
_RULES: Dict[str, Callable[[float], str]] = {
    "en": _english,
    "zh": _chinese,
}


def plural_category(language: str, n: float) -> str:
    """Return the CLDR plural category of *n* for *language* (default: English)."""
    rule = _RULES.get(language, _english)
    return rule(n)


def register_rule(language: str, rule: Callable[[float], str]) -> None:
    """Register a CLDR plural rule for *language* (extension point for new locales)."""
    _RULES[language] = rule


__all__ = ["plural_category", "register_rule", "ZERO", "ONE", "TWO", "FEW", "MANY", "OTHER"]
