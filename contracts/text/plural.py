"""Deterministic English count-noun pluralisation.

The rule ``"s" if n != 1 else ""`` was re-derived inline at ~11 call sites
(``grep``, ``read``, ``write``, ``run_graph``, ``ruff``, ``_browser``, ...), and
several sites gave up and hard-coded the lazy ``"line(s)"`` form — which renders the
ungrammatical ``"1 line(s)"``. Homing the rule here lets every count-noun render
correctly with one call.

Zero dependencies beyond the stdlib; no I/O, no provider shapes, no rendering. This
is deliberately English-only: the codebase is single-language and has no i18n seam,
so a locale-parameterised design would be speculative debt, not future-proofing.
"""
from __future__ import annotations


def plural(noun: str, n: int) -> str:
    """Return the singular or plural form of *noun* for count *n*.

    ``n == 1`` yields the singular; every other count (including ``0`` and negatives)
    yields the plural — matching English usage (``0 files``, ``1 file``, ``2 files``).
    Every call site so far pluralises by appending ``"s"``; if an irregular ever shows
    up, add the branch then, not speculatively now.
    """
    return noun if n == 1 else noun + "s"


def count_noun(n: int, noun: str) -> str:
    """Render ``"<n> <noun>"`` with the noun pluralised for *n* (e.g. ``"1 file"``,
    ``"3 files"``)."""
    return f"{n} {plural(noun, n)}"


def verb_agree(n: int, singular: str, plural_form: str) -> str:
    """Pick the verb form that agrees with count *n* (``was``/``were``, ``is``/``are``).

    The same ``singular if n == 1 else plural`` subject-verb agreement was inlined
    across a handful of tool notices (``read``, ``edit``, ``_browser``). Unlike
    :func:`plural`, English verb pairs are irregular, so both forms are passed in.
    """
    return singular if n == 1 else plural_form


__all__ = ["plural", "count_noun", "verb_agree"]
