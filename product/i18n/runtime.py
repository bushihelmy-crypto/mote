#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The active-locale runtime: catalog registry + ``t()`` lookup/fallback/format.

The active locale lives in a :class:`~contextvars.ContextVar`, which is:

* **async-safe** — each task sees its own locale, no cross-talk;
* **test-safe** — :func:`use_locale` scopes a locale to a ``with`` block and
  restores the previous one on exit (also used by ``/lang`` previews).

``set_locale`` sets the process-wide default; ``t(key, **params)`` looks the key
up in the active locale's catalog (``code`` → ``language`` → :data:`BASE_LOCALE`),
renders it with the ICU-subset formatter + the locale's CLDR plural rule, and —
on a truly missing key — returns a visible ``⟦key⟧`` marker instead of crashing.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from mote.product.i18n.detect import resolve_locale_code
from mote.product.i18n.locale import Locale, available_locales, get_locale, negotiate
from mote.product.i18n.message import format_message

#: Default when nothing is configured — keeps the current Chinese CLI UX.
DEFAULT_LOCALE = "zh"
#: Ultimate catalog fallback so a missing translation still renders (complete).
BASE_LOCALE = "en"

# msg-id → pattern, keyed by locale code and populated through register_catalog.
_CATALOGS: Dict[str, Dict[str, str]] = {}

_active: ContextVar[Locale] = ContextVar("mote_active_locale")


def register_catalog(code: str, mapping: Mapping[str, str]) -> None:
    """Register (merge) a locale's ``{msg-id: pattern}`` catalog under *code*."""
    _CATALOGS.setdefault(code, {}).update(mapping)


def _default_locale() -> Locale:
    return get_locale(DEFAULT_LOCALE) or negotiate(DEFAULT_LOCALE)


def current_locale() -> Locale:
    """The active :class:`Locale` (falls back to the configured default)."""
    return _active.get(None) or _default_locale()


def set_locale(code: Optional[str]) -> Locale:
    """Set the process-wide active locale from a tag; return the resolved locale."""
    loc = negotiate(code, default=DEFAULT_LOCALE)
    _active.set(loc)
    return loc


@contextmanager
def use_locale(code: Optional[str]) -> Iterator[Locale]:
    """Scope the active locale to a ``with`` block (tests + ``/lang`` preview)."""
    token = _active.set(negotiate(code, default=DEFAULT_LOCALE))
    try:
        yield _active.get()
    finally:
        _active.reset(token)


def negotiate_and_set(
    config_language: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Locale:
    """Resolve (config → env) then set the process locale; return the locale."""
    code = resolve_locale_code(config_language, environ)
    return set_locale(code)


def locales() -> Tuple[str, ...]:
    """The codes of every shipped locale."""
    return available_locales()


def _lookup(loc: Locale, key: str) -> Optional[str]:
    for code in (loc.code, loc.language, BASE_LOCALE):
        catalog = _CATALOGS.get(code)
        if catalog is not None and key in catalog:
            return catalog[key]
    return None


def t(key: str, **params: Any) -> str:
    """Translate *key* under the active locale, rendering *params* (never raises).

    A missing key returns a visible ``⟦key⟧`` marker (so a gap is obvious in the
    UI + fails the catalog-completeness test) rather than crashing the host.
    """
    loc = current_locale()
    pattern = _lookup(loc, key)
    if pattern is None:
        return f"\u27e6{key}\u27e7"
    return format_message(pattern, params, loc)


__all__ = [
    "DEFAULT_LOCALE",
    "BASE_LOCALE",
    "register_catalog",
    "current_locale",
    "set_locale",
    "use_locale",
    "negotiate_and_set",
    "locales",
    "t",
]
